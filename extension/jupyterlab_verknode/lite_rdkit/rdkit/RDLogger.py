"""rdkit.RDLogger in the browser version: the logs of RDKit's WebAssembly build."""


def DisableLog(name: str = "rdApp.*"):
    try:
        from ._core import rdkit

        rdkit().disable_logging()
    except Exception:  # noqa: BLE001 - (RDKit not loaded yet)
        pass


def EnableLog(name: str = "rdApp.*"):
    try:
        from ._core import rdkit

        rdkit().enable_logging()
    except Exception:  # noqa: BLE001
        pass


def logger():
    class _Logger:
        def setLevel(self, *_):
            pass

    return _Logger()


CRITICAL, ERROR, WARNING, INFO, DEBUG = 4, 3, 2, 1, 0
