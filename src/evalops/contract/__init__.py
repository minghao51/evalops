"""The evalops contract layer: stable, versioned shapes for everything evalops persists.

`evalops.contract.formats` owns `FORMAT_VERSION`, the `_meta` block, and the readers/writers that
make older files readable. Everything else in the package imports from here instead of inventing
its own version field.
"""
