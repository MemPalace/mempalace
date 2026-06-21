# Spec: `mempalace/sources/__init__.py`

## Purpose

This file is the public package entry point for the source-adapter subsystem (RFC 002). It defines no behavior of its own beyond aggregating and re-exporting names from three sibling submodules so that consumers import the entire source-adapter public surface from a single module path (`mempalace/sources/__init__.py:L1-L17`).

## Public Surface (Re-exported Contract)

The package re-exports a fixed set of names from submodule `base`: the per-source read-side contract base class `BaseSourceAdapter`; the typed records `SourceRef`, `SourceItemMetadata`, `DrawerRecord`, `RouteHint`, `SourceSummary`, `AdapterSchema`, `FieldSpec`, `IngestMode`, and `IngestResult`; and the error types `SourceAdapterError`, `SourceNotFoundError`, `AuthRequiredError`, `AdapterClosedError`, `TransformationViolationError`, and `SchemaConformanceError` (`mempalace/sources/__init__.py:L19-L36`).

The package re-exports `PalaceContext` (the facade core passes to adapters during ingest) and `ProgressHook` from submodule `context` (`mempalace/sources/__init__.py:L37`).

The package re-exports the adapter registry functions from submodule `registry`: `register`, `unregister`, `get_adapter`, `get_adapter_class`, `available_adapters`, `resolve_adapter_for_source`, and `reset_adapters` (`mempalace/sources/__init__.py:L38-L46`).

## Exported Name Contract (Observable)

The module declares an explicit export list naming exactly the following symbols, which constitutes the package's documented public API: `AdapterClosedError`, `AdapterSchema`, `AuthRequiredError`, `BaseSourceAdapter`, `DrawerRecord`, `FieldSpec`, `IngestMode`, `IngestResult`, `PalaceContext`, `ProgressHook`, `RouteHint`, `SchemaConformanceError`, `SourceAdapterError`, `SourceItemMetadata`, `SourceNotFoundError`, `SourceRef`, `SourceSummary`, `TransformationViolationError`, `available_adapters`, `get_adapter`, `get_adapter_class`, `register`, `reset_adapters`, `resolve_adapter_for_source`, and `unregister` (`mempalace/sources/__init__.py:L48-L74`).

Every symbol named in the export list is bound by one of the three re-export statements; the export list and the re-imported names are consistent with each other (`mempalace/sources/__init__.py:L19-L74`). Note that `SourceAdapterError` is imported and exported (`mempalace/sources/__init__.py:L30`, `mempalace/sources/__init__.py:L61`) but is not mentioned in the module-level documentation block.

## Invariants

- Importing this package must succeed only if all three submodules (`base`, `context`, `registry`) successfully provide every listed name; a missing name in any submodule is a hard import failure (`mempalace/sources/__init__.py:L19-L46`).
- The set of importable public names from this package is exactly the union of the three import statements, and the curated export list governs wildcard-style consumption (`mempalace/sources/__init__.py:L19-L74`).

## Side Effects, Errors, Inputs/Outputs

This module takes no inputs, produces no outputs, performs no filesystem, network, process, or environment side effects, and defines no callable logic of its own; it is purely a name-aggregation surface (`mempalace/sources/__init__.py:L1-L75`). The behavior of each re-exported symbol is defined by its originating submodule (`base`, `context`, or `registry`) and is out of scope for this file.
