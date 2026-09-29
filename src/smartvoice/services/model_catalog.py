"""Compatibility facade for model metadata and lifecycle operations."""

from smartvoice.services.model_catalog_constants import (
    MAX_ARCHIVE_BYTES, MAX_ARCHIVE_MEMBERS, MAX_EXTRACTED_BYTES, MODEL_ID_PATTERN,
)
from smartvoice.domain.models import ModelSpec
from smartvoice.services.model_registry import get_model_spec, load_catalog, supported_language_codes
from smartvoice.services.model_storage import (
    catalog_models, export_model, import_model, installed_models, model_directory, model_storage, uninstall_model,
)
from smartvoice.services.model_download import (
    ModelDownloadCancelled, _download, _download_once, _safe_extract, install_model,
)
from smartvoice.services.file_integrity import sha256 as _sha256
