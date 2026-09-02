"""from_weights no debe filtrar los print() de InsightFace a stdout.

stdout es el flujo de eventos JSON-lines del hub; si InsightFace escribe ahí
al cargar los ONNX ("Applied providers:", "find model:", "set det-size:",
etc.) corrompe el flujo. Se stubea insightface.app.FaceAnalysis (el import
real carga modelos ONNX de disco) para verificar la redirección sin
depender de tener los pesos instalados.
"""
from __future__ import annotations

import sys
import types

from vitahub.identity.insightface_engine import InsightFaceEngine


class _FakeFaceAnalysis:
    """Imita el ruido real de FaceAnalysis: imprime en __init__ y en prepare()."""

    def __init__(self, name: str, root: str, allowed_modules: list[str]) -> None:
        print("Applied providers: ['CPUExecutionProvider']")
        print(f"find model: {root}/models/{name}/det.onnx detection")
        print("model ignore: recognition_landmark")
        self.name = name
        self.root = root
        self.allowed_modules = allowed_modules
        self.prepared_with: tuple[int, tuple[int, int]] | None = None

    def prepare(self, ctx_id: int, det_size: tuple[int, int]) -> None:
        print("set det-size: (640, 640)")
        self.prepared_with = (ctx_id, det_size)


def test_from_weights_does_not_print_to_stdout(monkeypatch, capsys):
    fake_module = types.ModuleType("insightface.app")
    fake_module.FaceAnalysis = _FakeFaceAnalysis  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "insightface.app", fake_module)
    monkeypatch.setitem(sys.modules, "insightface", types.ModuleType("insightface"))

    engine = InsightFaceEngine.from_weights("/tmp/pesos")

    assert capsys.readouterr().out == ""
    assert isinstance(engine._app, _FakeFaceAnalysis)  # type: ignore[attr-defined]
    assert engine._app.prepared_with == (0, (640, 640))  # type: ignore[attr-defined]
