from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from spectrum_organizer.safety.fingerprints import snapshot_sources
from spectrum_organizer.safety.source_copies import copy_sources, ensure_sufficient_space
from spectrum_organizer.workflow.extraction_contracts import (
    ApprovedPreExtractionRunContext,
)


def prepare_extraction_context(
    *,
    selected_source_paths,
    output_parent,
    settings_snapshot: Mapping[str, object],
    protected_paths,
    ownership,
    timestamp: str,
    free_bytes_provider: Callable[[Path], int] | None = None,
    copy_file: Callable[[Path, Path], None] | None = None,
) -> ApprovedPreExtractionRunContext:
    source_paths = tuple(Path(path) for path in selected_source_paths)
    protected = tuple(Path(path) for path in protected_paths)
    snapshots = tuple(snapshot_sources(list(source_paths), list(protected)))
    protected_snapshots = tuple(snapshot_sources(list(protected), []))
    input_total = sum(snapshot.size_bytes for snapshot in snapshots)
    ensure_sufficient_space(ownership.temp_root, input_total, free_bytes_provider)
    copy_result = copy_sources(
        list(snapshots),
        ownership,
        free_bytes_provider=free_bytes_provider,
        copy_file=copy_file,
    )
    return ApprovedPreExtractionRunContext(
        run_id=ownership.run_id,
        timestamp=timestamp,
        selected_source_paths=source_paths,
        output_parent=Path(output_parent),
        settings_snapshot=dict(settings_snapshot),
        source_fingerprints_before=snapshots,
        temp_root=copy_result.ownership.temp_root,
        temp_root_identity=copy_result.ownership.temp_root_identity,
        run_owned_source_copy_paths=tuple(copy.path for copy in copy_result.copies),
        protected_fingerprints_before=protected_snapshots,
    )
