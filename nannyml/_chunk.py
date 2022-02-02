# Author:   Niels Nuyttens  <niels@nannyml.com>
#           Jakub Bialek    <jakub@nannyml.com>
#
# License: Apache Software License 2.0

import abc
import logging
from typing import List

import pandas as pd

from nannyml.exceptions import ChunkerException, InvalidArgumentsException

# from dateutil.parser import ParserError  # dropped due to pre-commit issues with dateutil types.


logger = logging.getLogger(__name__)


class Chunk:
    """A subset of data that acts as a logical unit during calculations."""

    def __init__(self, key: str, data: pd.DataFrame, partition: str = None):
        self.key = key
        self.data = data
        self.partition = partition

        self.is_transition: bool = False

    def __repr__(self):
        return (
            f'Chunk[key={self.key}, data=pd.DataFrame[[{self.data.shape[0]}x{self.data.shape[1]}]], '
            f'partition={self.partition}, is_transition={self.is_transition}]'
        )

    def __len__(self):
        return self.data.shape[0]


def _minimal_chunk_count(data: pd.DataFrame) -> int:
    return data.shape[0] // 5


def _is_transition(c: Chunk, partition_column_name: str) -> bool:
    if c.data.shape[0] > 1:
        return c.data[partition_column_name].nunique() > 1
    else:
        return False


class Chunker(abc.ABC):
    """Base class for Chunker implementations.

    Inheriting classes will split a DataFrame into a list of Chunks.
    They will do this based on several constraints, e.g. observation timestamps, number of observations per Chunk
    or a preferred number of Chunks.
    """

    def __init__(self, partition_column_name: str = 'partition'):
        self.partition_column_name = partition_column_name

    def split(self, data: pd.DataFrame) -> List[Chunk]:
        chunks = self._split(data)

        for c in chunks:
            if _is_transition(c, self.partition_column_name):
                c.is_transition = True

        if len(chunks) < 6:
            # TODO wording
            logger.warning(
                'The resulting number of chunks is too low.'
                'Please consider splitting your data in a different way or continue at your own risk.'
            )

        # check if all chunk sizes > minimal chunk size. If not, render a warning message.
        underpopulated_chunks = [c for c in chunks if len(c) < _minimal_chunk_count(data)]

        if len(underpopulated_chunks) > 0:
            # TODO wording
            logger.warning(
                f'The resulting list of chunks contains {len(underpopulated_chunks)} underpopulated chunks.'
                'They contain too few records to be statistically relevant and might negatively influence '
                'the quality of calculations.'
                'Please consider splitting your data in a different way or continue at your own risk.'
            )

        return chunks

    # TODO wording
    @abc.abstractmethod
    def _split(self, data: pd.DataFrame) -> List[Chunk]:
        """Perform the actual splitting of the DataFrame into Chunks

        Abstract method, to be implemented within inheriting classes.

        Parameters
        ----------
        data: pandas.DataFrame
            The full dataset that should be split into Chunks

        Returns
        -------
        chunks: array of Chunks
            The array of Chunks after splitting the original DataFrame `data`

        See also
        --------
        PeriodBasedChunker: Splits data based on the timestamp of observations
        SizeBasedChunker: Splits data based on the amount of observations in a Chunk
        CountBasedChunker: Splits data based on the resulting number of Chunks

        Notes
        -----

        There is a minimal number of observations that a Chunk should contain in order to retain statistical relevance.
        A chunker will log a warning message when your splitting criteria would result in underpopulated chunks.
        Note that in this situation calculation results may not be relevant.

        """
        pass


class PeriodBasedChunker(Chunker):
    """A Chunker that will split data into Chunks based on a date column in the data.

    Examples
    --------

    Chunk using monthly periods and providing a column name

    >>> from nannyml._chunk import PeriodBasedChunker
    >>> df = pd.read_parquet('/path/to/my/data.pq')
    >>> chunker = PeriodBasedChunker(date_column_name='observation_date', offset='M')
    >>> chunks = chunker.split(data=df)

    Or chunk using weekly periods

    >>> from nannyml._chunk import PeriodBasedChunker
    >>> df = pd.read_parquet('/path/to/my/data.pq')
    >>> chunker = PeriodBasedChunker(date_column=df['observation_date'], offset='W')
    >>> chunks = chunker.split(data=df)

    """

    def __init__(
        self,
        date_column_name: str = None,
        date_column: pd.Series = None,
        offset: str = 'W',
        partition_column_name: str = 'partition',
    ):
        """
        Parameters
        ----------

        date_column_name: string
            The name of the column in the DataFrame that contains the date used for chunking.
            Required in case `date_column` is not specified, raises InvalidArgumentsException otherwise.

        date_column: pd.Series
            The column of the given DataFrame that contains the date used for chunking.
            Required in case `date_column_name` is not specified, raises InvalidArgumentsException otherwise.

        offset: a frequency string representing a pandas.tseries.offsets.DateOffset
            The offset determines how the time-based grouping will occur. A list of possible values
            is to be found at https://pandas.pydata.org/docs/user_guide/timeseries.html#offset-aliases.

        partition_column_name: str
            The name of the column containing the partition of the observation. Defaults to `partition`.

        Returns
        -------

        chunker: a PeriodBasedChunker instance used to split data into time-based Chunks.

        """
        super().__init__(partition_column_name)
        if date_column is None and not date_column_name:
            raise InvalidArgumentsException(
                'date_column and date_column_name cannot both be None. Provide a value for one of both.'
            )

        if date_column is not None:
            self.date_column = date_column
        if date_column_name:
            self.date_column_name = date_column_name

        self.offset = offset

    def _split(self, data: pd.DataFrame) -> List[Chunk]:
        chunks = []
        date_column_name = self.date_column_name or self.date_column.name
        try:
            grouped_data = data.groupby(pd.to_datetime(data[date_column_name]).dt.to_period(self.offset))
            for k in grouped_data.groups.keys():
                chunks.append(Chunk(key=str(k), data=grouped_data.get_group(k)))
        except KeyError:
            raise ChunkerException(f"could not find date_column '{date_column_name}' in given data")

        # Had to drop this check due to issues with dateparser types during pre-commit checks
        #
        # except ParserError:
        #     raise ChunkerException(
        #         f"could not parse date_column '{date_column_name}' values as dates."
        #         f"Please verify if you've specified the correct date column."
        #     )

        except Exception as exc:
            raise ChunkerException(f"could not split data into chunks: {exc}")
        return chunks


class SizeBasedChunker(Chunker):
    """A Chunker that will split data into Chunks based on the preferred number of observations per Chunk.

    Notes
    -----

    - Chunks are adjacent, not overlapping
    - There will be no "incomplete chunks", so the leftover observations that cannot fill an entire chunk will
      be dropped by default.

    Examples
    --------

    Chunk using monthly periods and providing a column name

    >>> from nannyml._chunk import SizeBasedChunker
    >>> df = pd.read_parquet('/path/to/my/data.pq')
    >>> chunker = SizeBasedChunker(chunk_size=2000)
    >>> chunks = chunker.split(data=df)

    """

    def __init__(self, chunk_size: int, partition_column_name: str = 'partition'):
        """
        Parameters
        ----------

        chunk_size: int
            The preferred size of the resulting Chunks, i.e. the number of observations in each Chunk.

        partition_column_name: str
            The name of the column containing the partition of the observation. Defaults to `partition`.

        Returns
        -------

        chunker: a size-based instance used to split data into Chunks of a constant size.

        """
        super().__init__(partition_column_name)

        # TODO wording
        if not isinstance(chunk_size, int):
            raise InvalidArgumentsException(
                f"given chunk_size is of type {type(chunk_size)} but should be an int."
                f"Please provide an integer as a chunk size"
            )

        # TODO wording
        if chunk_size <= 0:
            raise InvalidArgumentsException(
                f"given chunk_size {chunk_size} is less then or equal to zero."
                f"The chunk size should always be larger then zero"
            )

        self.chunk_size = chunk_size

    def _split(self, data: pd.DataFrame) -> List[Chunk]:
        chunks = [
            Chunk(key=f'[{i}:{i + self.chunk_size - 1}]', data=data.loc[i : i + self.chunk_size - 1, :])
            for i in range(0, len(data), self.chunk_size)
            if i + self.chunk_size - 1 < len(data)
        ]

        return chunks


class CountBasedChunker(Chunker):
    """Base class for Chunker implementations.

    Examples
    --------


    """

    def __init__(self, chunk_count: int, partition_column_name: str = 'partition'):
        """ """
        super().__init__(partition_column_name)

        # TODO wording
        if not isinstance(chunk_count, int):
            raise InvalidArgumentsException(
                f"given chunk_count is of type {type(chunk_count)} but should be an int."
                f"Please provide an integer as a chunk count"
            )

        # TODO wording
        if chunk_count <= 0:
            raise InvalidArgumentsException(
                f"given chunk_count {chunk_count} is less then or equal to zero."
                f"The chunk count should always be larger then zero"
            )

        self.chunk_count = chunk_count

    def _split(self, data: pd.DataFrame) -> List[Chunk]:
        if data.shape[0] == 0:
            return []

        chunk_size = data.shape[0] // self.chunk_count
        chunks = [
            Chunk(key=f'[{i}:{i + chunk_size - 1}]', data=data.loc[i : i + chunk_size - 1, :])
            for i in range(0, len(data), chunk_size)
            if i + chunk_size - 1 < len(data)
        ]
        return chunks
