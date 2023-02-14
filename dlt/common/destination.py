from abc import ABC, abstractmethod
from importlib import import_module
from types import TracebackType, ModuleType
from typing import Any, Callable, ClassVar, Final, List, Optional, Literal, Type, Protocol, Union, TYPE_CHECKING, cast

from dlt.common.configuration.utils import serialize_value
from dlt.common.exceptions import IdentifierTooLongException, InvalidDestinationReference, UnknownDestinationModule
from dlt.common.schema import Schema
from dlt.common.schema.typing import TTableSchema
from dlt.common.configuration import configspec
from dlt.common.configuration.specs import BaseConfiguration, CredentialsConfiguration, ContainerInjectableContext
from dlt.common.configuration.accessors import config


# known loader file formats
# jsonl - new line separated json documents
# puae-jsonl - internal extract -> normalize format bases on jsonl
# insert_values - insert SQL statements
TLoaderFileFormat = Literal["jsonl", "puae-jsonl", "insert_values"]


@configspec(init=True)
class DestinationCapabilitiesContext(ContainerInjectableContext):
    """Injectable destination capabilities required for many Pipeline stages ie. normalize"""
    preferred_loader_file_format: TLoaderFileFormat
    supported_loader_file_formats: List[TLoaderFileFormat]
    escape_identifier: Callable[[str], str]
    escape_literal: Callable[[Any], Any]
    max_identifier_length: int
    max_column_identifier_length: int
    max_query_length: int
    is_max_query_length_in_bytes: bool
    max_text_data_type_length: int
    is_max_text_data_type_length_in_bytes: bool
    supports_ddl_transactions: bool

    # do not allow to create default value, destination caps must be always explicitly inserted into container
    can_create_default: ClassVar[bool] = False


def generic_destination_capabilities() ->DestinationCapabilitiesContext:
    caps = DestinationCapabilitiesContext()
    caps.preferred_loader_file_format=None
    caps.supported_loader_file_formats = ["jsonl", "insert_values"]
    caps.escape_identifier = lambda x: x
    caps.escape_literal = lambda x: serialize_value(x)
    caps.max_identifier_length = 65536
    caps.max_column_identifier_length = 65536
    caps.max_query_length = 32 * 1024 * 1024
    caps.is_max_query_length_in_bytes = True
    caps.max_text_data_type_length = 1024 * 1024 * 1024
    caps.is_max_text_data_type_length_in_bytes = True
    caps.supports_ddl_transactions = True
    return caps


@configspec(init=True)
class DestinationClientConfiguration(BaseConfiguration):
    destination_name: str = None  # which destination to load data to
    credentials: Optional[CredentialsConfiguration]

    if TYPE_CHECKING:
        def __init__(self, destination_name: str = None, credentials: Optional[CredentialsConfiguration] = None) -> None:
            ...


@configspec(init=True)
class DestinationClientDwhConfiguration(DestinationClientConfiguration):
    # keep default/initial value if present
    dataset_name: Final[str] = None
    """dataset name in the destination to load data to, for schemas that are not default schema, it is used as dataset prefix"""
    default_schema_name: Optional[str] = None
    """name of default schema to be used to name effective dataset to load data to"""

    if TYPE_CHECKING:
        def __init__(
            self,
            destination_name: str = None,
            credentials: Optional[CredentialsConfiguration] = None,
            dataset_name: str = None,
            default_schema_name: Optional[str] = None
        ) -> None:
            ...


TLoadJobStatus = Literal["running", "failed", "retry", "completed"]


class LoadJob:
    """Represents a job that loads a single file

        Each job starts in "running" state and ends in one of terminal states: "retry", "failed" or "completed".
        Each job is uniquely identified by a file name. The file is guaranteed to exist in "running" state. In terminal state, the file may not be present.
        In "running" state, the loader component periodically gets the state via `status()` method. When terminal state is reached, load job is discarded and not called again.
        `exception` method is called to get error information in "failed" and "retry" states.

        The `__init__` method is responsible to put the Job in "running" state. It may raise `LoadClientTerminalException` and `LoadClientTransientException` to
        immediately transition job into "failed" or "retry" state respectively.
    """
    def __init__(self, file_name: str) -> None:
        """
        File name is also a job id (or job id is deterministically derived) so it must be globally unique
        """
        self._file_name = file_name

    @abstractmethod
    def status(self) -> TLoadJobStatus:
        pass

    @abstractmethod
    def file_name(self) -> str:
        pass

    @abstractmethod
    def exception(self) -> str:
        pass


class JobClientBase(ABC):

    capabilities: ClassVar[DestinationCapabilitiesContext] = None

    def __init__(self, schema: Schema, config: DestinationClientConfiguration) -> None:
        self.schema = schema
        self.config = config

    @abstractmethod
    def initialize_storage(self) -> None:
        pass

    @abstractmethod
    def is_storage_initialized(self) -> bool:
        pass

    def update_storage_schema(self) -> None:
        self._verify_schema_identifier_lengths()

    @abstractmethod
    def start_file_load(self, table: TTableSchema, file_path: str) -> LoadJob:
        pass

    @abstractmethod
    def restore_file_load(self, file_path: str) -> LoadJob:
        pass

    @abstractmethod
    def complete_load(self, load_id: str) -> None:
        pass

    @abstractmethod
    def __enter__(self) -> "JobClientBase":
        pass

    @abstractmethod
    def __exit__(self, exc_type: Type[BaseException], exc_val: BaseException, exc_tb: TracebackType) -> None:
        pass

    def _verify_schema_identifier_lengths(self) -> None:
        """Checks all table and column name lengths against destination capabilities"""
        for table in self.schema.all_tables():
            table_name = table["name"]
            if len(table_name) > self.capabilities.max_identifier_length:
                raise IdentifierTooLongException(self.config.destination_name, "table", table_name, self.capabilities.max_identifier_length)
            for column in table["columns"].values():
                column_name = column["name"]
                if len(column_name) > self.capabilities.max_column_identifier_length:
                    raise IdentifierTooLongException(
                        self.config.destination_name,
                        "column",
                        f"{table_name}.{column_name}",
                        self.capabilities.max_column_identifier_length
                    )


TDestinationReferenceArg = Union["DestinationReference", ModuleType, None, str]


class DestinationReference(Protocol):
    __name__: str

    def capabilities(self) -> DestinationCapabilitiesContext:
        ...

    def client(self, schema: Schema, initial_config: DestinationClientConfiguration = config.value) -> "JobClientBase":
        ...

    def spec(self) -> Type[DestinationClientConfiguration]:
        ...

    @staticmethod
    def from_name(destination: TDestinationReferenceArg) -> "DestinationReference":
        if destination is None:
            return None

        # if destination is a str, get destination reference by dynamically importing module
        if isinstance(destination, str):
            try:
                if "." in destination:
                    # this is full module name
                    destination_ref = cast(DestinationReference, import_module(destination))
                else:
                    # from known location
                    destination_ref = cast(DestinationReference, import_module(f"dlt.destinations.{destination}"))
            except ImportError:
                raise UnknownDestinationModule(destination)
        else:
            destination_ref = cast(DestinationReference, destination)

        # make sure the reference is correct
        try:
            c = destination_ref.spec()
            c.credentials
        except Exception:
            raise InvalidDestinationReference(destination)

        return destination_ref
