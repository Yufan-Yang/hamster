"""Regression checks run by smoke.py on its isolated database; no AI calls."""
from unittest.mock import patch

from shiguang import llm


def answer(tags, brief="一部日本成人剧情影片。"):
    return dict.fromkeys(llm.CLASSIFY_FIELDS) | {
        "title": "Test item", "creator": "演员甲", "library": "Videos", "folder": "Other",
        "brief": brief, "tags": tags, "needs_transcript": False,
    }


def classify_with(replies):
    with patch.object(llm, "LLM_API_KEY", "test"), patch.object(llm, "update"), \
            patch.object(llm, "library_tags", return_value=[]), \
            patch.object(llm, "llm_json", side_effect=replies) as model:
        result = llm.classify(1, "test.mp4", {}, {})
    return result, model


def invalid_tags():
    for tags in (None, "演员甲", [], ["演员甲"], ["演员甲"] * 3, ["演员甲", "日本", 1],
                 ["演员甲", "日本", "剧情"], ["成人视频", "日本", " "]):
        try:
            llm.validate_classification_tags(answer(tags))
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid tags: {tags}")
    llm.validate_classification_tags(answer(["成人视频", "日本", "演员甲"]))


def retry_feedback():
    result, model = classify_with([answer(["演员甲"]), answer(["成人视频", "日本", "演员甲"])])
    assert model.call_count == 2
    assert "Previous classification was invalid" in model.call_args.args[1]
    assert "成人视频" in result["tags"] and "note" not in result


def incomplete_fallback():
    result, model = classify_with([answer(["演员甲"]), answer(["演员甲"])])
    assert model.call_count == 2
    assert result["tags"] == ["演员甲", "成人视频"]
    assert "need review" in result["note"]
    assert result["title"] == "Test item"
    assert result["summary"] == "一部日本成人剧情影片。"
    result, _ = classify_with([answer(None), answer(None)])
    assert "成人视频" in result["tags"]
    result, _ = classify_with([RuntimeError("offline"), RuntimeError("offline")])
    assert "classification failed" in result["note"]


def avoid_topic_false_positives():
    for brief in ("一位教师的课程。", "演员甲出演的访谈。", "关于成人影片产业的纪录片。", "这并非成人影片。"):
        assert llm.required_classification_tags({"brief": brief}) == []
    assert llm.required_classification_tags({"brief": "演员甲主演的日本成人影片。"}) == ["成人视频"]


def run(check):
    check("classification rejects missing, malformed and incomplete tags", invalid_tags)
    check("classification retries with validation feedback", retry_feedback)
    check("incomplete classification preserves known category and flags review", incomplete_fallback)
    check("category recovery does not infer adult content from people or industry discussion", avoid_topic_false_positives)
