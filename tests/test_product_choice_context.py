"""「上一轮要什么，这一轮补型号」的回归测试。

生产现场（审计记录 12:04）：
  1 发我0653的视频        → 反问「请补充具体型号」（命中 5 款，超过候选上限）
  2 LY0653GDT            → 回了整个产品详情   ← 原始请求（要视频）被遗忘
  3 发我视频              → 又问「要哪个产品的视频？」
  4 LY0653GDT            → 又回整个详情
  5 发我LY0653GDT的产品视频 → 才发出来

根因两处：候选超过 3 款时 `_ask_product_choice` 清空状态且什么都不记；
以及缩小候选时完整型号被「只是包含它」的变体拖住，只剩一个候选却仍在追问。
"""

from app.business.intent import Intent, IntentDecision, MediaAction
from app.business.service import BusinessAgent
from app.business.store import BusinessResult

FAMILY = ["LY0653GDT", "LY0653GDT-T", "LY0653GDT-Y", "LY0653NBT", "LY0653NBZ"]


class _Store:
    def __init__(self, models: list[str]) -> None:
        self.models = models
        self.by_id = {model: self._result(model) for model in models}

    @staticmethod
    def _result(model: str) -> BusinessResult:
        return BusinessResult(
            source_type="product",
            source_id=model,
            title=model,
            content=f"产品型号：{model}；套装价：100.00元；材质：ABS外壳铜水路",
            score=1.0,
        )

    def search_products(self, *_args, **_kwargs):  # pragma: no cover - 本文件不触发检索
        return []

    def get_active_product(self, identifier: str):
        return self.by_id.get(str(identifier))


def _ask_decision() -> IntentDecision:
    return IntentDecision(
        intent=Intent.PRODUCT_MEDIA,
        confidence=0.97,
        reason="员工要产品视频",
        query_for_search="发我0653的视频",
        source="deterministic_fast_path",
        media_action=MediaAction.SEND,
        media_type="video",
        media_role="product_video",
    )


def _fallback() -> IntentDecision:
    # 裸型号会被规则判成「看这个产品」，这正是需要被上一轮意图覆盖的情况
    return IntentDecision(
        intent=Intent.PRODUCT,
        confidence=0.9,
        reason="员工提到具体型号",
        query_for_search="LY0653GDT",
        source="rules",
    )


def _agent() -> BusinessAgent:
    return BusinessAgent(_Store(FAMILY))


def _products(agent: BusinessAgent) -> list[BusinessResult]:
    return [agent.store.get_active_product(model) for model in FAMILY]


def test_too_many_candidates_still_records_the_original_request() -> None:
    """候选超限时不展示编号列表，但原始请求必须记下来。

    此前这里是 `_state_pop(...)` + 直接返回，什么都不记，于是员工补型号时
    原始请求（要视频）已经被遗忘。
    """
    agent = _agent()

    agent._ask_product_choice("u1", _products(agent), "发我0653的视频", _ask_decision())

    pending = agent._state_get(agent._product_choices, "u1")
    assert isinstance(pending, dict)
    assert pending["choices_shown"] is False
    assert pending["intent"] == str(Intent.PRODUCT_MEDIA)
    assert pending["media_action"] == str(MediaAction.SEND)
    assert pending["media_role"] == "product_video"


def test_supplying_a_model_resumes_the_original_media_request() -> None:
    """员工补一个完整型号，应当继续上一轮那件事（发视频），而不是展示整个产品。"""
    agent = _agent()
    agent._ask_product_choice("u1", _products(agent), "发我0653的视频", _ask_decision())

    resumed = agent._resume_product_choice("u1", "LY0653GDT", _fallback())

    assert resumed is not None
    assert resumed.intent == Intent.PRODUCT_MEDIA
    assert resumed.media_action == MediaAction.SEND
    assert resumed.media_role == "product_video"
    # 检索要用这一句（型号在里面），而不是上一轮那句模糊的
    assert resumed.query_for_search == "LY0653GDT"
    pending = agent._state_get(agent._product_choices, "u1")
    assert pending["selected_id"] == "LY0653GDT"


def test_complete_model_wins_over_containing_variants() -> None:
    """「LY0653GDT」不能同时被 -T / -Y 拖住，否则只剩一个正确答案却还在追问。"""
    agent = _agent()
    agent._ask_product_choice("u1", _products(agent), "发我0653的视频", _ask_decision())

    agent._resume_product_choice("u1", "LY0653GDT", _fallback())

    pending = agent._state_get(agent._product_choices, "u1")
    assert pending["selected_id"] == "LY0653GDT"


def test_a_numeric_choice_is_rejected_when_no_list_was_shown() -> None:
    """候选超限时没有展示编号列表，员工不可能在选编号；不能拿 ids[index] 去猜。"""
    agent = _agent()
    agent._ask_product_choice("u1", _products(agent), "发我0653的视频", _ask_decision())

    assert agent._resume_product_choice("u1", "第二个", _fallback()) is None
    assert agent._state_get(agent._product_choices, "u1") is None
