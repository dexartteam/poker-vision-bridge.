import json

import httpx
import pytest

from app.vision.provider import MockProvider, OpenAIProvider, RecognitionError


async def test_provider_strict_schema_image_and_secret_stay_server_side(
    profile, frame_factory, observation
):
    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert body["store"] is False and body["text"]["format"]["strict"] is True
        assert body["input"][0]["content"][1]["image_url"] == frame_factory().image
        assert body["text"]["format"]["schema"]["additionalProperties"] is False
        assert set(body["text"]["format"]["schema"]["required"]) == set(
            body["text"]["format"]["schema"]["properties"]
        )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": observation.model_dump_json(),
                            }
                        ],
                    }
                ],
            },
        )

    provider = OpenAIProvider("test-secret", "test-model", transport=httpx.MockTransport(handler))
    try:
        result, raw = await provider.recognize(frame_factory(), profile)
        assert result == observation and "test-secret" not in json.dumps(raw)
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "response,code",
    [
        ({"status": "incomplete", "output": []}, "provider_incomplete"),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "secret upstream"}],
                    }
                ],
            },
            "provider_refusal",
        ),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "not JSON"}],
                    }
                ],
            },
            "provider_invalid_output",
        ),
        ({"status": "completed", "output": []}, "provider_invalid_output"),
    ],
)
async def test_provider_bad_output_never_falls_back_to_mock(profile, frame_factory, response, code):
    provider = OpenAIProvider(
        "secret",
        "test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    )
    try:
        with pytest.raises(RecognitionError, match=code):
            await provider.recognize(frame_factory(), profile)
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "status,code",
    [
        (429, "provider_rate_limited"),
        (401, "provider_unavailable"),
        (500, "provider_unavailable"),
    ],
)
async def test_provider_http_errors_do_not_expose_body(profile, frame_factory, status, code):
    provider = OpenAIProvider(
        "secret",
        "test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="private-upstream-secret")
        ),
    )
    try:
        with pytest.raises(RecognitionError) as error:
            await provider.recognize(frame_factory(), profile)
        assert str(error.value) == code
    finally:
        await provider.close()


async def test_provider_timeout_safe_code(profile, frame_factory):
    def timeout(_):
        raise httpx.ReadTimeout("Authorization secret")

    provider = OpenAIProvider("secret", "test", transport=httpx.MockTransport(timeout))
    try:
        with pytest.raises(RecognitionError, match="^provider_timeout$"):
            await provider.recognize(frame_factory(), profile)
    finally:
        await provider.close()


async def test_provider_mock_explicitly_returns_unknown(profile, frame_factory):
    result, raw = await MockProvider().recognize(frame_factory(), profile)
    assert result.confidence == 0 and result.pot.value is None and raw["mode"] == "mock"
