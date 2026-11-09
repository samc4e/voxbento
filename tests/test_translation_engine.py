"""Tests for the TranslationEngine interface (#1184) and the local NLLB engine behind it."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from portal.translations import worker as worker_module
from portal.translations.engine import TranslationContext, TranslationEngine, TranslationLanguage
from portal.translations.providers.local import LocalProvider
from portal.translations.worker import TranslationWorker

ENGLISH = TranslationLanguage(code="en", name="English")
FRENCH = TranslationLanguage(code="fr", name="French")
GERMAN = TranslationLanguage(code="de", name="German")
NLLB = "nllb-200-distilled-600M"


class FakeEngine(TranslationEngine):
    """Returns "<target code>:<text>" for every pair and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[TranslationLanguage, list[TranslationLanguage], list[str], TranslationContext]] = []

    async def translate_batch(
        self,
        source_lang: TranslationLanguage,
        targets: Sequence[TranslationLanguage],
        texts: Sequence[str],
        context: TranslationContext,
    ) -> list[list[str | None]]:
        self.calls.append((source_lang, list(targets), list(texts), context))
        return [[f"{target.code}:{text}" for target in targets] for text in texts]


class NoTranslationEngine(TranslationEngine):
    async def translate_batch(self, source_lang, targets, texts, context):
        return [[None for _ in targets] for _ in texts]


class FailingEngine(TranslationEngine):
    async def translate_batch(self, source_lang, targets, texts, context):
        raise RuntimeError("engine crashed")


def test_an_engine_must_implement_translate_batch():
    class Incomplete(TranslationEngine):
        pass

    with pytest.raises(TypeError, match="translate_batch"):
        Incomplete()
    assert isinstance(FakeEngine(), TranslationEngine)


def test_context_repr_hides_the_api_key():
    context = TranslationContext(model="gpt-4o-mini", api_key="sk-secret-value")

    assert "sk-secret-value" not in repr(context)
    assert context.api_key == "sk-secret-value"


def test_local_provider_is_registered_as_an_engine():
    assert isinstance(worker_module.ENGINES["local"], LocalProvider)
    assert isinstance(worker_module.ENGINES["local"], TranslationEngine)
    # A name in both registries would never reach its provider.
    assert not worker_module.ENGINES.keys() & worker_module.PROVIDERS.keys()


@pytest.mark.anyio
async def test_worker_runs_local_translation_through_the_registered_nllb_engine():
    local_engine = worker_module.ENGINES["local"]
    worker = TranslationWorker(broadcast_callback=None)

    with (
        patch.object(local_engine, "translate_batch", wraps=local_engine.translate_batch) as translate_batch,
        patch.object(local_engine, "_run_inference", return_value="Bonjour") as inference,
    ):
        result = await worker._call_llm(
            "local", NLLB, None, "Hello", "French", "English", target_lang_code="fr", source_language_code="en"
        )

    assert result == "Bonjour"
    translate_batch.assert_awaited_once()
    inference.assert_called_once_with("Hello", "eng_Latn", "fra_Latn", NLLB)


@pytest.mark.anyio
async def test_worker_passes_segment_and_settings_to_the_engine():
    engine = FakeEngine()
    entry = SimpleNamespace(source_term="Voxbento", target_term="Voxbento", description=None)
    worker = TranslationWorker(broadcast_callback=None)

    with (
        patch.dict(worker_module.ENGINES, {"local": engine}),
        patch.object(LocalProvider, "_run_inference", side_effect=AssertionError("NLLB must not run")),
    ):
        result = await worker._call_llm(
            "local",
            "nllb-test",
            "unused-key",
            "Hello",
            "French",
            "English",
            target_lang_code="fr",
            source_language_code="en",
            persona="A conference interpreter.",
            style="Formal register.",
            vocabulary_entries=[entry],
        )

    assert result == "fr:Hello"
    assert engine.calls == [
        (
            ENGLISH,
            [FRENCH],
            ["Hello"],
            TranslationContext(
                model="nllb-test",
                api_key="unused-key",
                persona="A conference interpreter.",
                style="Formal register.",
                vocabulary_entries=(entry,),
            ),
        )
    ]


@pytest.mark.anyio
async def test_worker_uses_any_registered_engine_and_defaults_missing_values():
    """An engine registered under a new name is used without any NLLB code, and a room
    without a floor language reaches the engine as an empty code."""
    engine = FakeEngine()
    worker = TranslationWorker(broadcast_callback=None)

    with patch.dict(worker_module.ENGINES, {"fake-engine": engine}):
        result = await worker._call_llm(
            "fake-engine", "fake-model", None, "Hola", "German", "Spanish", target_lang_code="de"
        )

    assert result == "de:Hola"
    source, targets, texts, context = engine.calls[0]
    assert source == TranslationLanguage(code="", name="Spanish")
    assert targets == [GERMAN]
    assert texts == ["Hola"]
    assert context == TranslationContext(model="fake-model")


@pytest.mark.anyio
async def test_worker_reports_a_failed_pair_as_no_translation(caplog):
    worker = TranslationWorker(broadcast_callback=None)

    with patch.dict(worker_module.ENGINES, {"local": NoTranslationEngine()}):
        result = await worker._call_llm("local", "nllb-test", None, "Hello", "French", "English", target_lang_code="fr")

    assert result is None
    assert "Provider local returned empty translation" in caplog.text


@pytest.mark.anyio
async def test_worker_lets_engine_errors_reach_the_pipeline():
    worker = TranslationWorker(broadcast_callback=None)

    with patch.dict(worker_module.ENGINES, {"local": FailingEngine()}):
        with pytest.raises(RuntimeError, match="engine crashed"):
            await worker._call_llm("local", "nllb-test", None, "Hello", "French", "English", target_lang_code="fr")


@pytest.mark.anyio
async def test_unknown_provider_is_still_rejected(caplog):
    worker = TranslationWorker(broadcast_callback=None)

    result = await worker._call_llm("no-such-provider", "m", None, "Hello", "French", "English")

    assert result is None
    assert "Translation provider no-such-provider not supported." in caplog.text


@pytest.mark.anyio
async def test_local_engine_runs_one_inference_at_a_time_in_order():
    provider = LocalProvider()
    outputs = {"fra_Latn": "Bonjour", "deu_Latn": "Hallo"}
    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0

    def fake_inference(text, source_token, target_token, model_size):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.02)  # long enough for concurrent calls to overlap
        with lock:
            in_flight -= 1
        return f"{outputs[target_token]} ({text})"

    with patch.object(provider, "_run_inference", side_effect=fake_inference) as inference:
        results = await provider.translate_batch(
            ENGLISH, [FRENCH, GERMAN], ["Hello", "Hi"], TranslationContext(model=NLLB)
        )

    assert results == [["Bonjour (Hello)", "Hallo (Hello)"], ["Bonjour (Hi)", "Hallo (Hi)"]]
    assert [call.args for call in inference.call_args_list] == [
        ("Hello", "eng_Latn", "fra_Latn", NLLB),
        ("Hello", "eng_Latn", "deu_Latn", NLLB),
        ("Hi", "eng_Latn", "fra_Latn", NLLB),
        ("Hi", "eng_Latn", "deu_Latn", NLLB),
    ]
    assert max_in_flight == 1


@pytest.mark.anyio
async def test_local_engine_marks_only_the_unsupported_target_as_failed():
    provider = LocalProvider()
    unsupported = TranslationLanguage(code="xx", name="UnknownLanguage")

    with patch.object(provider, "_run_inference", return_value="Bonjour") as inference:
        results = await provider.translate_batch(
            ENGLISH, [unsupported, FRENCH], ["Hello"], TranslationContext(model=NLLB)
        )

    assert results == [[None, "Bonjour"]]
    inference.assert_called_once_with("Hello", "eng_Latn", "fra_Latn", NLLB)


@pytest.mark.anyio
async def test_local_engine_forwards_every_setting_to_translate():
    provider = LocalProvider()
    entry = SimpleNamespace(source_term="Voxbento", target_term="Voxbento", description=None)
    context = TranslationContext(
        model=NLLB, api_key="k", persona="Persona.", style="Style.", vocabulary_entries=[entry]
    )

    with patch.object(provider, "translate", new_callable=AsyncMock, return_value="Bonjour") as translate:
        results = await provider.translate_batch(ENGLISH, [FRENCH], ["Hello"], context)

    assert results == [["Bonjour"]]
    translate.assert_awaited_once_with(
        provider_name="local",
        text="Hello",
        target_lang_name="French",
        target_lang_code="fr",
        source_lang_name="English",
        model=NLLB,
        api_key="k",
        persona="Persona.",
        style="Style.",
        vocabulary_entries=[entry],
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("targets", "texts", "expected"),
    [([FRENCH], [], []), ([], ["Hello"], [[]])],
    ids=["no-texts", "no-targets"],
)
async def test_local_engine_handles_empty_batches_without_inference(targets, texts, expected):
    provider = LocalProvider()

    with patch.object(provider, "_run_inference", side_effect=AssertionError("no pair to translate")):
        results = await provider.translate_batch(ENGLISH, targets, texts, TranslationContext(model=NLLB))

    assert results == expected
