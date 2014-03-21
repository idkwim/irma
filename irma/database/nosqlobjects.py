from common.compat import timestamp
from irma.common.utils import IrmaLock, IrmaLockMode
from nosqlhandler import NoSQLDatabase
from bson import ObjectId
from bson.errors import InvalidId
from irma.common.exceptions import IrmaDatabaseError, IrmaLockError, IrmaValueError


class NoSQLDatabaseObjectList(object):
    # TODO derived class from UserList to handle list of databaseobject, group remove, group update...
    pass

class NoSQLDatabaseObject(object):
    """ Generic class to map an object to a db entry
    load will create an object from a db entry
    _save will create/update a db entry with object's values"""
    _uri = None
    _dbname = None
    _collection = None

    # List of transient members of the class (see to_dict)
    _transient_attributes = [
        '_temp_id',
        '_is_instance_transient'
    ]

    def __init__(self, id=None, mode=IrmaLockMode.read, save=True):
        """ Constructor. Note: the object is being saved during the creation process.
        :param id: the id of the object to load
        :param mode: the mode for the lock on the object, use IrmaLockMode.read to be able read the object
                        (the object might be locked in write mode somewhere elsewhere) or IrmaLockMode.write
                        to block write operations on the current object from other threads (read operations remain
                        possible)
        :param save: if the object has to be saved, use only for temporary
                    objects (you'll not be able to save it after the instantiation)
        :raise: IrmaDatabaseError, IrmaLockError, IrmaValueError
        """
        if type(self) is NoSQLDatabaseObject:
            raise IrmaValueError('The NoSQLDatabaseObject class has to be overloaded; it cannot be instantiated')

        self._is_instance_transient = not save  # transient (see _transient_attributes and to_dict)

        # create a new object or load an existing one with id
        # raise IrmaDatabaseError on loading invalid id
        self._id = None
        self._temp_id = None
        self._lock = None
        self._lock_time = None
        if id:
            try:
                self._id = ObjectId(id)
                self._temp_id = self._id  # transient (see _transient_attributes and to_dict)
                self.load(self._id)
            except InvalidId as e:
                raise IrmaDatabaseError("{0}".format(e))
        elif save:
                self._save()

        if mode == IrmaLockMode.read:
            # if an id is provided there is nothing to do
            if not id:
                self._lock = IrmaLock.free
                self._lock_time = 0
                if save:
                    self.update({'_lock': self._lock, '_lock_time': self._lock_time})
        elif mode == IrmaLockMode.write:
            if not id:
                self._lock = IrmaLock.locked
                self._lock_time = timestamp()
            if save or id:
                self.take(mode)
        else:
            raise IrmaValueError('The lock mode {0} is not available'.format(mode))

    # TODO: Add support for both args and kwargs
    def from_dict(self, dict_object):
        for k, v in dict_object.items():
            setattr(self, k, v)

    # See http://stackoverflow.com/questions/1305532/ to handle it in a generic way
    def to_dict(self):
        """Converts object to dict.
        :rtype: dict
        """
        # from http://stackoverflow.com/questions/61517/python-dictionary-from-an-objects-fields
        return dict((key, getattr(self, key)) for key in dir(self) if key not in dir(self.__class__) and getattr(self, key) is not None and key not in self._transient_attributes and key != self._transient_attributes)

    def update(self, update_dict={}):
        """Update the current instance in the db, be sure to have the lock on the object before updating (ne verifications are being made)
        :param update_dict: the attributes/values to update in the bd, the whole
                object is being updated if nothing is provided
        :rtype: None
        """

        db = NoSQLDatabase(self._dbname, self._uri)

        if self._id != self._temp_id:    # if the id is being changed, create a new instance
            old_id = self._temp_id
            self._temp_id = self._id
            self._save()
            db.remove(self._dbname, self._collection, old_id)
        else:
            if update_dict == {}:
                update_dict = self.to_dict()
                del update_dict['_id']
            db.update(self._dbname, self._collection, self._id, update_dict)
        return

    def _save(self):
        db = NoSQLDatabase(self._dbname, self._uri)
        self._id = db.save(self._dbname, self._collection, self.to_dict())
        self._temp_id = self._id
        return

    def load(self, _id):
        self._id = _id
        db = NoSQLDatabase(self._dbname, self._uri)
        dict_object = db.load(self._dbname, self._collection, self._id)
        # dict_object could be empty if we init a dbobject with a given id
        if dict_object:
            self.from_dict(dict_object)
            self._temp_id = self._id
        else:
            raise IrmaDatabaseError("id not present in collection")
        return

    def remove(self):
        db = NoSQLDatabase(self._dbname, self._uri)
        db.remove(self._dbname, self._collection, self._id)
        return

    def _update_lock(self, lock):
        self._lock = lock
        self._lock_time = timestamp()
        self.update({'_lock': self._lock, '_lock_time': self._lock_time})

    def release(self):
        """ Release the lock on the current instance. Note: it is possible to release a lock even if it hasn't been locked by the current thread.
        """
        if self._lock != IrmaLock.free:
            self._update_lock(IrmaLock.free)

    def take(self, mode=IrmaLockMode.write):
        """ Take the lock on the current instance

        :param mode: The mode of the lock. Note: only the w (write) mode is available for the moment
        :rtype: Boolean
        :return: True if the object stored in db is different from the corresponding instance in the program,
                False otherwise
        :raise: IrmaLockError, IrmaLockModeError
        """
        ret = self.has_state_changed()

        if mode == IrmaLockMode.write:
            if self.__class__.is_lock_free(self.id) or self.__class__.has_lock_timed_out(self.id):
                self._update_lock(IrmaLock.locked)
                return ret
            raise IrmaLockError('The lock on {0} n{1} has already been taken'.format(self.__class__.__name__, self.id))
        raise IrmaValueError('The lock mode {0} is not available'.format(mode))

    @classmethod
    def has_lock_timed_out(cls, id):
        """Check if the lock on the object with the current id has timed out or not
        :param id: the id of the object in the db
        :rtype: Boolean
        :return: True is the lock has timed out, False otherwise
        :raise: NotImplementedError if called from the mother class
        """
        if cls is NoSQLDatabaseObject:
            raise NotImplementedError('has_lock_timed_out must be overloaded in the subclasses')
        return IrmaLock.lock_timeout < timestamp() - cls(id=id, save=False)._lock_time

    @classmethod
    def is_lock_free(cls, id):
        """Check if the lock for the given id has been taken or not
        :param id: the id of the object in the db
        :rtype: Boolean
        :return: True is the lock is free, False otherwise
        :raise: NotImplementedError if called from the mother class
        """
        if cls is NoSQLDatabaseObject:
            raise NotImplementedError('has_lock_timed_out must be overloaded in the subclasses')
        return cls(id=id, save=False)._lock == IrmaLock.free

    def has_state_changed(self):
        """Check if the state of the object has changed between two locks (doesn't take the lock into account)
        :param id: the id of the object to test
        :rtype: Boolean
        :return: True if the objects are different, False otherwise
        """
        from_db = self.__class__(id=self.id, save=False).to_dict()

        del from_db['_lock']
        del from_db['_lock_time']
        from_instance = self.to_dict()
        del from_instance['_lock']
        del from_instance['_lock_time']

        return from_instance != from_db

    @classmethod
    def get_temp_instance(cls, id):
        """Return a transient instance of the object corresponding to the given id
        :param id: the id of the object to return
        :rtype: NoSQLDatabaseObject
        :return: The transient object
        """
        if cls is NoSQLDatabaseObject:
            raise NotImplementedError('get_temp_instance must be overloaded in the subclasses')
        return cls(id=id, save=False)

    @property
    def id(self):
        """Return str version of ObjectId"""
        if not self._id:
            return None
        else:
            return str(self._id)

    @id.setter
    def id(self, value):
        self._id = ObjectId(value)

    @classmethod
    def init_id(cls, id):
        _id = ObjectId(id)
        db = NoSQLDatabase(cls._dbname, cls._uri)
        if db.exists(cls._dbname, cls._collection, _id):
            new_object = cls(id=id)
        else:
            new_object = cls()
            new_object.id = id
        return new_object

    def __repr__(self):
        return str(self.to_dict())

    def __str__(self):
        return str(self.to_dict())