"""numpy, imported on first use.

The backend registry imports every backend module, so a module-level
``import numpy`` in one of them costs each ``mempalace`` start tens of
milliseconds, including commands and hooks that never touch a vector.
``from ._numpy import np`` reads like the usual alias and defers the import
to the first attribute access.
"""


class _Numpy:
    def __getattr__(self, name: str):
        import numpy

        return getattr(numpy, name)


np = _Numpy()
