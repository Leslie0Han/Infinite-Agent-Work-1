import asyncio
import importlib
import sys
from pathlib import Path

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]


def check_online_image_count_contract():
    sys.path.insert(0, str(ROOT))
    app_main = importlib.import_module("main")

    for invalid_n in (0, 5):
        try:
            app_main.OnlineImageRequest(prompt="test", n=invalid_n)
        except ValidationError:
            pass
        else:
            raise AssertionError("online image count must stay within the supported 1..4 contract")

    calls = []
    saved_results = []
    original_get_provider = app_main.get_api_provider
    original_generate = app_main.generate_ai_image
    original_save_output = app_main.save_ai_image_to_output
    original_save_history = app_main.save_to_history
    original_loop = app_main.GLOBAL_LOOP

    async def fake_generate(*args):
        index = len(calls) + 1
        calls.append(args)
        return {"type": "url", "value": f"https://example.test/{index}.png"}, {"id": f"request-{index}"}

    async def fake_save_output(image_data, prefix="online_", category="output"):
        return f"/assets/output/{Path(image_data['value']).name}"

    try:
        app_main.get_api_provider = lambda provider_id: {
            "id": provider_id,
            "name": "Test provider",
            "image_models": ["test-image-model"],
        }
        app_main.generate_ai_image = fake_generate
        app_main.save_ai_image_to_output = fake_save_output
        app_main.save_to_history = saved_results.append
        app_main.GLOBAL_LOOP = None

        result = asyncio.run(app_main.build_online_image_result(
            app_main.OnlineImageRequest(prompt="three useful alternatives", provider_id="test", n=3)
        ))
    finally:
        app_main.get_api_provider = original_get_provider
        app_main.generate_ai_image = original_generate
        app_main.save_ai_image_to_output = original_save_output
        app_main.save_to_history = original_save_history
        app_main.GLOBAL_LOOP = original_loop

    assert len(calls) == 3, "count must fan out through the proven single-image path so every provider can honor it"
    assert result["images"] == [
        "/assets/output/1.png",
        "/assets/output/2.png",
        "/assets/output/3.png",
    ], "all requested alternatives must be returned instead of silently keeping only the first image"
    assert result["params"]["n"] == 3, "history must retain why one generation produced multiple alternatives"
    assert saved_results == [result], "one user request must remain one history item even when it contains multiple images"


if __name__ == "__main__":
    check_online_image_count_contract()
    print("online image count checks passed")
