from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from spectrum_organizer.safety.identity_paths import ProjectArtifactEvidence


class OriginStructureMismatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FormulaCalculationState:
    recalculation_mode: str
    lock_state: str


AUTOMATIC_FORMULA_LOCK_STATE = FormulaCalculationState(
    recalculation_mode="automatic",
    lock_state="formula_lock",
)


@dataclass(frozen=True)
class ColumnWriteContract:
    short_name: str
    designation: str
    comment: str
    values: tuple[Decimal | None, ...]
    formula: str | None = None
    method: str | None = None


@dataclass(frozen=True)
class BookWriteContract:
    display_long_name: str
    internal_short_name: str | None
    columns: tuple[ColumnWriteContract, ...]

    def with_replaced_column(
        self,
        index: int,
        column: ColumnWriteContract,
    ) -> "BookWriteContract":
        columns = list(self.columns)
        columns[index] = column
        return replace(self, columns=tuple(columns))


@dataclass(frozen=True)
class FolderWriteContract:
    path: str
    books: tuple[BookWriteContract, ...]

    def with_replaced_book(
        self,
        index: int,
        book: BookWriteContract,
    ) -> "FolderWriteContract":
        books = list(self.books)
        books[index] = book
        return replace(self, books=tuple(books))


@dataclass(frozen=True)
class ProjectWriteContract:
    root_path: str
    folders: tuple[FolderWriteContract, ...]

    def with_replaced_value(
        self,
        folder_index: int,
        book_index: int,
        column_index: int,
        row_index: int,
        value: Decimal | None,
    ) -> "ProjectWriteContract":
        column = self.folders[folder_index].books[book_index].columns[
            column_index
        ]
        values = list(column.values)
        values[row_index] = value
        return self._replace_column(
            folder_index,
            book_index,
            column_index,
            replace(column, values=tuple(values)),
        )

    def with_replaced_formula(
        self,
        folder_index: int,
        book_index: int,
        column_index: int,
        formula: str | None,
    ) -> "ProjectWriteContract":
        column = self.folders[folder_index].books[book_index].columns[
            column_index
        ]
        return self._replace_column(
            folder_index,
            book_index,
            column_index,
            replace(column, formula=formula),
        )

    def with_replaced_method(
        self,
        folder_index: int,
        book_index: int,
        column_index: int,
        method: str | None,
    ) -> "ProjectWriteContract":
        column = self.folders[folder_index].books[book_index].columns[
            column_index
        ]
        return self._replace_column(
            folder_index,
            book_index,
            column_index,
            replace(column, method=method),
        )

    def with_replaced_comment(
        self,
        folder_index: int,
        book_index: int,
        column_index: int,
        comment: str,
    ) -> "ProjectWriteContract":
        column = self.folders[folder_index].books[book_index].columns[
            column_index
        ]
        return self._replace_column(
            folder_index,
            book_index,
            column_index,
            replace(column, comment=comment),
        )

    def with_replaced_book_short_name(
        self,
        folder_index: int,
        book_index: int,
        short_name: str | None,
    ) -> "ProjectWriteContract":
        book = self.folders[folder_index].books[book_index]
        return self._replace_book(
            folder_index,
            book_index,
            replace(book, internal_short_name=short_name),
        )

    def with_removed_column(
        self,
        folder_index: int,
        book_index: int,
    ) -> "ProjectWriteContract":
        book = self.folders[folder_index].books[book_index]
        return self._replace_book(
            folder_index,
            book_index,
            replace(book, columns=book.columns[:-1]),
        )

    def _replace_column(
        self,
        folder_index: int,
        book_index: int,
        column_index: int,
        column: ColumnWriteContract,
    ) -> "ProjectWriteContract":
        book = self.folders[folder_index].books[book_index]
        return self._replace_book(
            folder_index,
            book_index,
            book.with_replaced_column(column_index, column),
        )

    def _replace_book(
        self,
        folder_index: int,
        book_index: int,
        book: BookWriteContract,
    ) -> "ProjectWriteContract":
        folder = self.folders[folder_index]
        folders = list(self.folders)
        folders[folder_index] = folder.with_replaced_book(book_index, book)
        return replace(self, folders=tuple(folders))
