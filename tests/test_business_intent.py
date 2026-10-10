import json
from types import SimpleNamespace

import pytest

from app.business.intent import (
    Intent,
    IntentDecision,
    MediaAction,
    classify_intent,
    classify_media_action,
    decide_intent,
    parse_intent_decision,
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("公司的打卡规定是什么", Intent.POLICY),
        ("客户问交期怎么回复", Intent.REPLY_SCRIPT),
        ("YY-1008是什么型号", Intent.PRODUCT),
        ("发我YY-1008的产品图片", Intent.PRODUCT_MEDIA),
        ("我想看看主体图", Intent.PRODUCT_MEDIA),
        ("有没有产品图", Intent.PRODUCT_MEDIA),
    ],
)
def test_classify_intent(query: str, expected: Intent) -> None:
    assert classify_intent(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我们现在一共有几条安装视频", MediaAction.COUNT),
        ("有哪些产品图片", MediaAction.LIST),
        ("我想看看主体图", MediaAction.SEND),
        ("把安装视频发我", MediaAction.SEND),
        ("发给我", MediaAction.SEND),
    ],
)
def test_classify_media_action(query: str, expected: MediaAction) -> None:
    assert classify_media_action(query) == expected


class FakeCompletions:
    def __init__(self, payload: dict[str, object] | str) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        content = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False)
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def fake_client(payload: dict[str, object] | str) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(payload)))


def test_semantic_intent_understands_goal_instead_of_customer_keyword() -> None:
    decision = decide_intent(
        "客户之前买的0689包含哪些配件？",
        client=fake_client(
            {
                "intent": "product_query",
                "confidence": 0.96,
                "reason": "员工在查询产品配件，而不是要求组织客户回复",
                "query_for_search": "查询0689产品包含的配件",
                "needs_clarification": False,
                "clarification_question": "",
                "media_action": None,
                "media_type": None,
                "media_role": None,
                "reply_scenario": None,
            }
        ),
        model="deepseek-chat",
    )

    assert decision.intent == Intent.PRODUCT
    assert decision.query_for_search == "查询0689产品包含的配件"
    assert decision.source == "model"


def test_semantic_intent_uses_context_to_rewrite_ellipsis() -> None:
    decision = decide_intent(
        "那安装视频也发我",
        recent_context=[
            {
                "role": "user",
                "content": "介绍一下0689",
                "intent": "product_query",
            }
        ],
        client=fake_client(
            {
                "intent": "product_media_query",
                "confidence": 0.98,
                "reason": "结合上一轮，员工要发送0689安装视频",
                "query_for_search": "发送0689产品的安装视频",
                "needs_clarification": False,
                "clarification_question": "",
                "media_action": "send",
                "media_type": "video",
                "media_role": "product_video",
                "reply_scenario": None,
            }
        ),
        model="deepseek-chat",
    )

    assert decision.intent == Intent.PRODUCT_MEDIA
    assert decision.media_action == MediaAction.SEND
    assert decision.media_role == "product_video"
    assert "0689" in decision.query_for_search


def test_low_confidence_semantic_intent_asks_before_wrong_route() -> None:
    decision = parse_intent_decision(
        json.dumps(
            {
                "intent": "general_knowledge",
                "confidence": 0.22,
                "reason": "问题过于简略",
                "query_for_search": "这个怎么办",
                "needs_clarification": False,
                "clarification_question": "",
            },
            ensure_ascii=False,
        ),
        "这个怎么办",
    )

    assert decision.needs_clarification is True
    assert "制度" in decision.clarification_question


def test_invalid_model_output_safely_falls_back_to_rules() -> None:
    decision = decide_intent(
        "把0689主体图发给我",
        recent_context=[
            {"role": "user", "content": "0689有哪些图片", "intent": "product_media_query"}
        ],
        client=fake_client("这是一段无法执行的普通文本"),
        model="deepseek-chat",
    )

    assert isinstance(decision, IntentDecision)
    assert decision.intent == Intent.PRODUCT_MEDIA
    assert decision.media_action == MediaAction.SEND
    assert decision.source == "rules"


@pytest.mark.parametrize("legacy_name", ["实物图", "主图"])
def test_legacy_image_name_is_rejected(legacy_name: str) -> None:
    client = fake_client(
        {
            "intent": "product_media_query",
            "confidence": 0.99,
            "query_for_search": "查询产品图片",
            "needs_clarification": False,
            "media_action": "list",
            "media_type": "image",
            "media_role": "body_image",
        }
    )

    decision = decide_intent(legacy_name, client=client, model="deepseek-chat")

    assert decision.intent == Intent.GENERAL
    assert decision.media_type is None
    assert decision.media_role is None
    assert decision.needs_clarification is True
    assert "没有识别到有效的图片类型" in decision.clarification_question
    assert "主体图" in decision.clarification_question
    assert len(client.chat.completions.calls) == 1


@pytest.mark.parametrize("legacy_name", ["实物图", "主图"])
def test_legacy_image_name_with_model_is_also_rejected(legacy_name: str) -> None:
    client = fake_client(
        {
            "intent": "product_media_query",
            "confidence": 0.99,
            "query_for_search": "查询产品主体图",
            "needs_clarification": False,
        }
    )

    decision = decide_intent(
        f"LY0686GT{legacy_name}", client=client, model="deepseek-chat"
    )

    assert decision.intent == Intent.GENERAL
    assert decision.needs_clarification is True
    assert "没有识别到有效的图片类型" in decision.clarification_question
    assert len(client.chat.completions.calls) == 1


def test_explicit_policy_question_uses_fast_path() -> None:
    """模型不可用时，明确的制度提问仍由规则快速路径正确判断。

    与 test_unambiguous_business_request_skips_model 同理：现在模型优先，
    规则只在没有模型或模型失败时出手；但规则这条能力本身必须保留。
    """
    decision = decide_intent("公司的迟到规定是什么？", client=None, model=None)

    assert decision.intent == Intent.POLICY
    assert decision.source == "deterministic_fast_path"


@pytest.mark.parametrize(
    ("query", "intent", "media_action", "media_role"),
    [
        ("LTA-822现在卖多少钱", Intent.PRODUCT, None, None),
        ("LTA-822有哪些配件", Intent.PRODUCT, None, None),
        ("把LTA-822的安装视频发我", Intent.PRODUCT_MEDIA, MediaAction.SEND, "product_video"),
        ("LTA-822有几张主体图", Intent.PRODUCT_MEDIA, MediaAction.COUNT, "body_image"),
        ("把LTA-822的产品图发我", Intent.PRODUCT_MEDIA, MediaAction.SEND, "product_image"),
        ("公司的营业执照在哪里", Intent.GENERAL, None, None),
        ("客户投诉花洒漏水，帮我回复", Intent.REPLY_SCRIPT, None, None),
    ],
)
def test_unambiguous_business_request_skips_model(query, intent, media_action, media_role):
    """**模型不可用时**，明确的请求仍然由规则快速路径给出同样正确的判断。

    这条以前断言的是「有模型也不许调用」——那是成本约定。现在改成**模型优先、规则兜底**：
    规则只认识已经写进词表的说法，让它抢先决定会让员工换一种说法就静默答错，
    所以内容理解交给模型。但规则本身的能力必须保留：一旦模型不可用（或调用失败），
    下面这些判断仍然要成立。判据从「有没有调用模型」改为「没有模型时结果一样」。
    """
    decision = decide_intent(query, client=None, model=None)

    assert decision.intent == intent
    assert decision.source == "deterministic_fast_path"
    assert decision.media_action == media_action
    assert decision.media_role == media_role


@pytest.mark.parametrize(
    "query",
    [
        "客户问LTA-822多少钱，我先查下准确价格",
        "客户嫌LTA-822价格太高，我怎么回复",
        "LTA-822多少钱，再把安装视频发我",
        "这个LTA-822多少钱",
    ],
)
def test_context_or_conflicting_signals_do_not_take_business_fast_path(query):
    client = fake_client("无法识别")
    decide_intent(query, client=client, model="deepseek-chat")

    assert len(client.chat.completions.calls) == 1


def test_low_confidence_customer_product_boundary_asks_targeted_question():
    decision = parse_intent_decision(
        json.dumps(
            {"intent": "product_query", "confidence": 0.2, "query_for_search": "0689价格"},
            ensure_ascii=False,
        ),
        "客户问0689价格，我该怎么办",
    )

    assert decision.needs_clarification
    assert "产品资料" in decision.clarification_question
    assert "回复" in decision.clarification_question


def test_existing_context_is_left_for_model_to_resolve():
    client = fake_client("无法识别")
    decide_intent(
        "LTA-822多少钱",
        recent_context=[{"role": "user", "content": "先说说产品", "intent": "product_query"}],
        client=client,
        model="deepseek-chat",
    )

    assert len(client.chat.completions.calls) == 1


def test_model_rewrite_cannot_replace_explicit_product_code():
    decision = parse_intent_decision(
        json.dumps(
            {
                "intent": "product_query",
                "confidence": 0.96,
                "query_for_search": "查询TTA-T01的价格",
            },
            ensure_ascii=False,
        ),
        "LTA-822多少钱",
    )

    assert decision.query_for_search == "LTA-822多少钱"


def test_model_rewrite_preserves_valid_explicit_product_code():
    decision = parse_intent_decision(
        json.dumps(
            {
                "intent": "product_query",
                "confidence": 0.96,
                "query_for_search": "查询LTA-822的销售价格",
            },
            ensure_ascii=False,
        ),
        "LTA-822多少钱",
    )

    assert decision.query_for_search == "查询LTA-822的销售价格"
