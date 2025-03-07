import logging
import os
import sys
import time
import warnings
from threading import local
from urllib.parse import quote, unquote, urlparse

from neo4j import DEFAULT_DATABASE, GraphDatabase, basic_auth
from neo4j.exceptions import ClientError, SessionExpired, ServiceUnavailable
from neo4j.graph import Node, Relationship
from retry import retry

from neomodel import config, core
from neomodel.exceptions import (
    ConstraintValidationFailed,
    NodeClassNotDefined,
    RelationshipClassNotDefined,
    UniqueProperty,
)

logger = logging.getLogger(__name__)


# make sure the connection url has been set prior to executing the wrapped function
def ensure_connection(func):
    def wrapper(self, *args, **kwargs):
        # Sort out where to find url
        if hasattr(self, "db"):
            _db = self.db
        else:
            _db = self

        if not _db.url:
            _db.set_connection(config.DATABASE_URL)
        return func(self, *args, **kwargs)

    return wrapper


def change_neo4j_password(db, new_password):
    db.cypher_query("CALL dbms.changePassword($password)", {"password": new_password})


def clear_neo4j_database(db, clear_constraints=False, clear_indexes=False):
    db.cypher_query("MATCH (a) DETACH DELETE a")
    if clear_constraints:
        core.drop_constraints()
    if clear_indexes:
        core.drop_indexes()


# pylint:disable=too-few-public-methods
class NodeClassRegistry:
    """
    A singleton class via which all instances share the same Node Class Registry.
    """

    # Maintains a lookup directory that is used by cypher_query
    # to infer which class to instantiate by examining the labels of the
    # node in the resultset.
    # _NODE_CLASS_REGISTRY is populated automatically by the constructor
    # of the NodeMeta type.
    _NODE_CLASS_REGISTRY = {}

    def __init__(self):
        self.__dict__["_NODE_CLASS_REGISTRY"] = self._NODE_CLASS_REGISTRY

    def __str__(self):
        ncr_items = list(
            map(
                lambda x: "{} --> {}".format(",".join(x[0]), x[1]),
                self._NODE_CLASS_REGISTRY.items(),
            )
        )
        return "\n".join(ncr_items)


class Database(local, NodeClassRegistry):
    """
    A singleton object via which all operations from neomodel to the Neo4j backend are handled with.
    """

    def __init__(self):
        """ """
        self._active_transaction = None
        self.url = None
        self.driver = None
        self._session = None
        self._pid = None
        self._database_name = DEFAULT_DATABASE
        self.protocol_version = None

    def set_connection(self, url):
        """
        Sets the connection URL to the address a Neo4j server is set up at
        """
        p_start = url.replace(":", "", 1).find(":") + 2
        p_end = url.rfind("@")
        password = url[p_start:p_end]
        url = url.replace(password, quote(password))
        parsed_url = urlparse(url)

        valid_schemas = [
            "bolt",
            "bolt+s",
            "bolt+ssc",
            "bolt+routing",
            "neo4j",
            "neo4j+s",
            "neo4j+ssc",
        ]

        if parsed_url.netloc.find("@") > -1 and parsed_url.scheme in valid_schemas:
            credentials, hostname = parsed_url.netloc.rsplit("@", 1)
            username, password = credentials.split(":")
            password = unquote(password)
            database_name = parsed_url.path.strip("/")
        else:
            raise ValueError(
                "Expecting url format: bolt://user:password@localhost:7687"
                " got {0}".format(url)
            )

        options = {
            "auth": basic_auth(username, password),
            "connection_acquisition_timeout": config.CONNECTION_ACQUISITION_TIMEOUT,
            "connection_timeout": config.CONNECTION_TIMEOUT,
            "keep_alive": config.KEEP_ALIVE,
            "max_connection_lifetime": config.MAX_CONNECTION_LIFETIME,
            "max_connection_pool_size": config.MAX_CONNECTION_POOL_SIZE,
            "max_transaction_retry_time": config.MAX_TRANSACTION_RETRY_TIME,
            "resolver": config.RESOLVER,
            "user_agent": config.USER_AGENT,
        }

        if "+s" not in parsed_url.scheme:
            options["encrypted"] = config.ENCRYPTED
            options["trust"] = config.TRUST

        self.driver = GraphDatabase.driver(
            parsed_url.scheme + "://" + hostname, **options
        )
        self.url = url
        self._pid = os.getpid()
        self._active_transaction = None
        self._database_name = DEFAULT_DATABASE if database_name == "" else database_name

    @property
    def transaction(self):
        """
        Returns the current transaction object
        """
        return TransactionProxy(self)

    @property
    def write_transaction(self):
        return TransactionProxy(self, access_mode="WRITE")

    @property
    def read_transaction(self):
        return TransactionProxy(self, access_mode="READ")

    @ensure_connection
    def begin(self, access_mode=None, **parameters):
        """
        Begins a new transaction, raises SystemError exception if a transaction is in progress
        """
        if self._active_transaction:
            raise SystemError("Transaction in progress")
        self._session = self.driver.session(
            default_access_mode=access_mode,
            database=self._database_name,
            **parameters,
        )
        self._active_transaction = self._session.begin_transaction()

    @ensure_connection
    def commit(self):
        """
        Commits the current transaction

        :return: last_bookmark
        """
        try:
            self._active_transaction.commit()
            last_bookmark = self._session.last_bookmark()
        finally:
            # In case when something went wrong during
            # committing changes to the database
            # we have to close an active transaction and session.
            self._active_transaction = None
            self._session = None

        return last_bookmark

    @ensure_connection
    def rollback(self):
        """
        Rolls back the current transaction
        """
        try:
            self._active_transaction.rollback()
        finally:
            # In case when something went wrong during changes rollback,
            # we have to close an active transaction and session
            self._active_transaction = None
            self._session = None

    def _object_resolution(self, result_list):
        """
        Performs in place automatic object resolution on a set of results
        returned by cypher_query.

        The function operates recursively in order to be able to resolve Nodes
        within nested list structures. Not meant to be called directly,
        used primarily by cypher_query.

        :param result_list: A list of results as returned by cypher_query.
        :type list:

        :return: A list of instantiated objects.
        """

        # Object resolution occurs in-place
        for a_result_item in enumerate(result_list):
            for a_result_attribute in enumerate(a_result_item[1]):
                try:
                    # Primitive types should remain primitive types,
                    # Nodes to be resolved to native objects
                    resolved_object = a_result_attribute[1]

                    # For some reason, while the type of `a_result_attribute[1]`
                    # as reported by the neo4j driver is `Node` for Node-type data
                    # retrieved from the database.
                    # When the retrieved data are Relationship-Type,
                    # the returned type is `abc.[REL_LABEL]` which is however
                    # a descendant of Relationship.
                    # Consequently, the type checking was changed for both
                    # Node, Relationship objects
                    if isinstance(a_result_attribute[1], Node):
                        resolved_object = self._NODE_CLASS_REGISTRY[
                            frozenset(a_result_attribute[1].labels)
                        ].inflate(a_result_attribute[1])

                    if isinstance(a_result_attribute[1], Relationship):
                        resolved_object = self._NODE_CLASS_REGISTRY[
                            frozenset([a_result_attribute[1].type])
                        ].inflate(a_result_attribute[1])

                    if isinstance(a_result_attribute[1], list):
                        resolved_object = self._object_resolution(
                            [a_result_attribute[1]]
                        )

                    result_list[a_result_item[0]][
                        a_result_attribute[0]
                    ] = resolved_object

                except KeyError as exc:
                    # Not being able to match the label set of a node with a known object results
                    # in a KeyError in the internal dictionary used for resolution. If it is impossible
                    # to match, then raise an exception with more details about the error.
                    if isinstance(a_result_attribute[1], Node):
                        raise NodeClassNotDefined(
                            a_result_attribute[1], self._NODE_CLASS_REGISTRY
                        ) from exc

                    if isinstance(a_result_attribute[1], Relationship):
                        raise RelationshipClassNotDefined(
                            a_result_attribute[1], self._NODE_CLASS_REGISTRY
                        ) from exc

        return result_list

    @retry(exceptions=(SessionExpired, ServiceUnavailable), tries=4, delay=0)
    @ensure_connection
    def cypher_query(
        self,
        query,
        params=None,
        handle_unique=True,
        retry_on_session_expire=True,
        resolve_objects=False,
    ):
        """
        Runs a query on the database and returns a list of results and their headers.

        :param query: A CYPHER query
        :type: str
        :param params: Dictionary of parameters
        :type: dict
        :param handle_unique: Whether or not to raise UniqueProperty exception on Cypher's ConstraintValidation errors
        :type: bool
        :param retry_on_session_expire: Whether or not to attempt the same query again if the transaction has expired
        :type: bool
        :param resolve_objects: Whether to attempt to resolve the returned nodes to data model objects automatically
        :type: bool
        """

        if self._pid != os.getpid():
            self.set_connection(self.url)

        if self._active_transaction:
            session = self._active_transaction
        else:
            session = self.driver.session(database=self._database_name)

        try:
            # Retrieve the data
            start = time.time()
            response = session.run(query, params)
            results, meta = [list(r.values()) for r in response], response.keys()
            end = time.time()

            if resolve_objects:
                # Do any automatic resolution required
                results = self._object_resolution(results)

        except ClientError as e:
            if e.code == "Neo.ClientError.Schema.ConstraintValidationFailed":
                if "already exists with label" in e.message and handle_unique:
                    raise UniqueProperty(e.message) from e

                raise ConstraintValidationFailed(e.message) from e
            exc_info = sys.exc_info()
            raise exc_info[1].with_traceback(exc_info[2])
        except (SessionExpired, ServiceUnavailable):
            if retry_on_session_expire:
                self.set_connection(self.url)
                return self.cypher_query(
                    query=query,
                    params=params,
                    handle_unique=handle_unique,
                    retry_on_session_expire=False,
                )
            raise

        tte = end - start
        if os.environ.get("NEOMODEL_CYPHER_DEBUG", False) and tte > float(
            os.environ.get("NEOMODEL_SLOW_QUERIES", 0)
        ):
            logger.debug(
                "query: "
                + query
                + "\nparams: "
                + repr(params)
                + "\ntook: {:.2g}s\n".format(tte)
            )

        return results, meta


class TransactionProxy:
    bookmarks = None

    def __init__(self, db, access_mode=None):
        self.db = db
        self.access_mode = access_mode

    @ensure_connection
    def __enter__(self):
        if self.bookmarks is None:
            self.db.begin(access_mode=self.access_mode)
        else:
            bookmarks = (
                (self.bookmarks,)
                if isinstance(self.bookmarks, (str, bytes))
                else tuple(self.bookmarks)
            )
            self.db.begin(access_mode=self.access_mode, bookmarks=bookmarks)
            self.bookmarks = None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value:
            self.db.rollback()

        if exc_type is ClientError:
            if exc_value.code == "Neo.ClientError.Schema.ConstraintValidationFailed":
                raise UniqueProperty(exc_value.message)

        if not exc_value:
            self.last_bookmark = self.db.commit()

    def __call__(self, func):
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper

    @property
    def with_bookmark(self):
        return BookmarkingTransactionProxy(self.db, self.access_mode)


class BookmarkingTransactionProxy(TransactionProxy):
    def __call__(self, func):
        def wrapper(*args, **kwargs):
            self.bookmarks = kwargs.pop("bookmarks", None)

            with self:
                result = func(*args, **kwargs)
                self.last_bookmark = None

            return result, self.last_bookmark

        return wrapper


def deprecated(message):
    # pylint:disable=invalid-name
    def f__(f):
        def f_(*args, **kwargs):
            warnings.warn(message, category=DeprecationWarning, stacklevel=2)
            return f(*args, **kwargs)

        f_.__name__ = f.__name__
        f_.__doc__ = f.__doc__
        f_.__dict__.update(f.__dict__)
        return f_

    return f__


def classproperty(f):
    class cpf:
        def __init__(self, getter):
            self.getter = getter

        def __get__(self, obj, type=None):
            return self.getter(type)

    return cpf(f)


# Just used for error messages
class _UnsavedNode:
    def __repr__(self):
        return "<unsaved node>"

    def __str__(self):
        return self.__repr__()


def _get_node_properties(node):
    """Get the properties from a neo4j.vx.types.graph.Node object."""
    return node._properties


def enumerate_traceback(initial_frame):
    depth, frame = 0, initial_frame
    while frame is not None:
        yield depth, frame
        frame = frame.f_back
        depth += 1
