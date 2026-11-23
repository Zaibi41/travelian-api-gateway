"""gateway#84: Parameter Store returns at most 10 parameters per call; the identity keys are read with the rest."""

import sys
import types
from unittest.mock import MagicMock, patch

from app.core import parameter_store
from app.core.config import settings


def test_parameters_are_read_in_batches_of_ten(monkeypatch):
    client = MagicMock()
    client.get_parameters.side_effect = lambda Names, WithDecryption: {
        "Parameters": [{"Name": name, "Value": "k1"} for name in Names if name.endswith("identity/active-key")], "InvalidParameters": []}
    boto3 = types.SimpleNamespace(client=lambda *a, **k: client)
    monkeypatch.setattr(settings, "ssm_parameter_prefix", "travelian/test")
    monkeypatch.setattr(settings, "pii_active_key", None)
    with patch.dict(sys.modules, {"boto3": boto3}):
        parameter_store.load_parameter_store_settings()
    sizes = [len(call.kwargs["Names"]) for call in client.get_parameters.call_args_list]
    assert sizes and max(sizes) <= 10 and sum(sizes) == len(parameter_store.PARAMETER_SETTINGS)
    assert "pii_keys" in parameter_store.PARAMETER_SETTINGS and settings.pii_active_key == "k1"
