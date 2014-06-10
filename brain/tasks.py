#
# Copyright (c) 2013-2014 QuarksLab.
# This file is part of IRMA project.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License in the top-level directory
# of this distribution and at:
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# No part of the project, including this file, may be copied,
# modified, propagated, or distributed except according to the
# terms contained in the LICENSE file.

import uuid
import time
from celery import Celery
import config.parser as config
from datetime import datetime, timedelta
from brain.objects import User, Scan
from lib.irma.common.utils import IrmaTaskReturn, IrmaScanStatus
from lib.irma.common.exceptions import IrmaTaskError, IrmaDatabaseError
from lib.irma.database.sqlhandler import SQLDatabase
from lib.irma.ftp.handler import FtpTls

# Time to cache the probe list
# to avoid asking to rabbitmq
PROBELIST_CACHE_TIME = 60
cache_probelist = {'list': None, 'time': None}

scan_app = Celery('scantasks')
config.conf_brain_celery(scan_app)

probe_app = Celery('probetasks')
config.conf_probe_celery(probe_app)

results_app = Celery('restasks')
config.conf_results_celery(results_app)

frontend_app = Celery('frontendtasks')
config.conf_frontend_celery(frontend_app)


# =============
#  SQL Helpers
# =============

def get_quota(sql, user):
    if user.quota == 0:
        # quota=0 means quota disabled
        quota = None
    else:
        # Quota are set per 24 hours
        delta = timedelta(hours=24)
        what = ("user_id={0} ".format(user.id) +
                "and date >= '{0}'".format(datetime.now() - delta))
        quota = user.quota - sql.sum(Scan.nbfiles, what)
    return quota


def get_groupresult(taskid):
    if not taskid:
        raise IrmaTaskError("BrainTask: task_id not set")
    gr = probe_app.GroupResult.restore(taskid)
    if not gr:
        raise IrmaTaskError("BrainTask: not a valid taskid")
    return gr


# ================
#  Celery Helpers
# ================

def route(sig):
    options = sig.app.amqp.router.route(
        sig.options, sig.task, sig.args, sig.kwargs,
    )
    try:
        queue = options.pop('queue')
    except KeyError:
        pass
    else:
        options.update(exchange=queue.exchange.name,
                       routing_key=queue.routing_key)
    sig.set(**options)
    return sig


# ===============
#  Tasks Helpers
# ===============

def get_probelist():
    now = time.time()
    result_queue = config.brain_config['broker_probe'].queue
    if cache_probelist['time'] is not None:
        cache_time = now - cache_probelist['time']
    if cache_probelist['time'] is None or cache_time > PROBELIST_CACHE_TIME:
        slist = list()
        i = probe_app.control.inspect()
        queues = i.active_queues()
        if queues:
            for infolist in queues.values():
                for info in infolist:
                    if info['name'] not in slist:
                        # exclude only predefined result queue
                        if info['name'] != result_queue:
                            slist.append(info['name'])
        cache_probelist['list'] = slist
        cache_probelist['time'] = now
    return cache_probelist['list']


def flush_dir(ftpuser, scanid):
    conf_ftp = config.brain_config['ftp_brain']
    with FtpTls(conf_ftp.host,
                conf_ftp.port,
                conf_ftp.username,
                conf_ftp.password) as ftps:
        ftps.deletepath("{0}/{1}".format(ftpuser, scanid), deleteParent=True)


# ===================
#  Tasks declaration
# ===================

@scan_app.task()
def probe_list():
    return IrmaTaskReturn.success(get_probelist())


@scan_app.task(ignore_result=True)
def scan(scanid, scan_request):
    engine = config.brain_config['sql_brain'].engine
    dbname = config.brain_config['sql_brain'].dbname
    sql = SQLDatabase(engine + dbname)
    available_probelist = get_probelist()
    jobs_list = []
    # FIXME: get rmq_vhost dynamically
    rmqvhost = config.brain_config['broker_frontend'].vhost
    try:
        user = sql.one_by(User, rmqvhost=rmqvhost)
        quota = get_quota(sql, user)
        if quota is not None:
            print("{0} : Found user {1} ".format(scanid, user.name) +
                  "quota remaining {0}/{1}".format(quota, user.quota))
        else:
            print "{0} : Found user {1} quota disabled".format(scanid,
                                                               user.name)
    except IrmaTaskError as e:
        return IrmaTaskReturn.error("BrainTask: {0}".format(e))

    for (filename, probelist) in scan_request:
        if probelist is None:
            return IrmaTaskReturn.error("BrainTask: Empty probe list")
        # first check probelist
        for p in probelist:
            # check if probe exists
            if p not in available_probelist:
                msg = "BrainTask: Unknown probe {0}".format(p)
                print ("{0}: Unknown probe {1}".format(scanid, p))
                return IrmaTaskReturn.error(msg)

        # Now, create one subtask per file to scan per probe according to quota
        for probe in probelist:
            if quota is not None and quota <= 0:
                break
            if quota:
                quota -= 1
            callback_signature = route(
                results_app.signature("brain.tasks.scan_result",
                                      (user.ftpuser, scanid, filename, probe)))
            jobs_list.append(
                probe_app.send_task("probe.tasks.probe_scan",
                                    args=(user.ftpuser, scanid, filename),
                                    queue=probe,
                                    link=callback_signature))

    if len(jobs_list) != 0:
        # Build a result set with all job AsyncResult
        # for progress/cancel operations
        groupid = str(uuid.uuid4())
        groupres = probe_app.GroupResult(id=groupid, results=jobs_list)
        # keep the groupresult object for task status/cancel
        groupres.save()

        scan = Scan(scanid=scanid,
                    taskid=groupid,
                    nbfiles=len(jobs_list),
                    status=IrmaScanStatus.launched,
                    user_id=user.id, date=datetime.now())
        sql.add(scan)
    print(
        "{0}: ".format(scanid) +
        "{0} files receives / ".format(len(scan_request)) +
        "{0} active probe / ".format(len(available_probelist)) +
        "{0} probe used / ".format(len(probelist)) +
        "{0} jobs launched".format(len(jobs_list)))
    return


@scan_app.task()
def scan_progress(scanid):
    try:
        engine = config.brain_config['sql_brain'].engine
        dbname = config.brain_config['sql_brain'].dbname
        sql = SQLDatabase(engine + dbname)
    except Exception as e:
        print ("{0}: sql error {1}".format(scanid, e))
        return IrmaTaskReturn.error("BrainTask: {0}".format(e))
    # FIXME: get rmq_vhost dynamically
    rmqvhost = config.brain_config['broker_frontend'].vhost
    try:
        user = sql.one_by(User, rmqvhost=rmqvhost)
    except Exception as e:
        print ("{0}: sql user not found {1}".format(scanid, e))
        msg = "BrainTask: sql user not found {0}".format(e)
        return IrmaTaskReturn.error(msg)
    try:
        scan = sql.one_by(Scan, scanid=scanid, user_id=user.id)
    except Exception as e:
        print ("{0}: sql scanid not found {1}".format(scanid, e))
        return IrmaTaskReturn.warning(IrmaScanStatus.created)
    if scan.status == IrmaScanStatus.launched:
        if not scan.taskid:
            return IrmaTaskReturn.error("task_id not set")
        gr = get_groupresult(scan.taskid)
        nbcompleted = nbsuccessful = 0
        for j in gr:
            if j.ready():
                nbcompleted += 1
            if j.successful():
                nbsuccessful += 1
        return IrmaTaskReturn.success({"total": len(gr),
                                       "finished": nbcompleted,
                                       "successful": nbsuccessful})
    else:
        return IrmaTaskReturn.warning(scan.status)


@scan_app.task()
def scan_cancel(scanid):
    try:
        engine = config.brain_config['sql_brain'].engine
        dbname = config.brain_config['sql_brain'].dbname
        sql = SQLDatabase(engine + dbname)
        # FIXME: get rmq_vhost dynamically
        rmqvhost = config.brain_config['broker_frontend'].vhost
        try:
            user = sql.one_by(User, rmqvhost=rmqvhost)
        except IrmaDatabaseError as e:
            return IrmaTaskReturn.error("User: {0}".format(e))
        try:
            scan = sql.one_by(Scan, scanid=scanid, user_id=user.id)
        except IrmaDatabaseError:
            print ("{0}: sql no scan with this id error {1}".format(scanid, e))
            return IrmaTaskReturn.warning(IrmaScanStatus.created)
        if scan.status == IrmaScanStatus.launched:
            scan.status = IrmaScanStatus.cancelling
            # commit as soon as possible to avoid cancelling again
            sql.commit()
            gr = get_groupresult(scan.taskid)
            nbcompleted = nbcancelled = 0
            # iterate over jobs in groupresult
            for j in gr:
                if j.ready():
                    nbcompleted += 1
                else:
                    j.revoke(terminate=True)
                    nbcancelled += 1
            scan.status = IrmaScanStatus.cancelled
            flush_dir(user.ftpuser, scanid)
            return IrmaTaskReturn.success({"total": len(gr),
                                           "finished": nbcompleted,
                                           "cancelled": nbcancelled})
        else:
            return IrmaTaskReturn.warning(scan.status)
    except IrmaTaskError as e:
        return IrmaTaskReturn.error("{0}".format(e))


@results_app.task(ignore_result=True)
def scan_result(result, ftpuser, scanid, filename, probe):
    try:
        frontend_app.send_task("frontend.tasks.scan_result",
                               args=(scanid, filename, probe, result))
        print "scanid {0} sent result {1}".format(scanid, probe)
        engine = config.brain_config['sql_brain'].engine
        dbname = config.brain_config['sql_brain'].dbname
        sql = SQLDatabase(engine + dbname)
        # FIXME get rmq_vhost dynamically
        rmqvhost = config.brain_config['broker_frontend'].vhost
        user = sql.one_by(User, rmqvhost=rmqvhost)
        scan = sql.one_by(Scan, scanid=scanid, user_id=user.id)
        gr = get_groupresult(scan.taskid)
        nbtotal = len(gr)
        nbcompleted = nbsuccessful = 0
        for j in gr:
            if j.ready():
                nbcompleted += 1
            if j.successful():
                nbsuccessful += 1
        if nbtotal == nbcompleted:
            scan.status = IrmaScanStatus.processed
            flush_dir(ftpuser, scanid)
            # delete groupresult
            gr.delete()
            print "{0} complete deleting files".format(scanid)
    except IrmaTaskError as e:
        return IrmaTaskReturn.error("{0}".format(e))
