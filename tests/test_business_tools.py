from app.business.tools import BusinessTools


class Owner:
    def _answer_knowledge(self, **kwargs):
        return ("knowledge", kwargs)

    def _answer_product(self, **kwargs):
        return ("product", kwargs)

    def _answer_media(self, **kwargs):
        return ("media", kwargs)

    def _record_intent_audit(self, *args, **kwargs):
        return ("audit", args, kwargs)


def test_business_tools_group_domain_boundaries_without_exposing_model_tools():
    tools = BusinessTools.for_owner(Owner())

    assert tools.knowledge.answer(query="制度")[0] == "knowledge"
    assert tools.product.answer(query="0689")[0] == "product"
    assert tools.media.answer(query="视频")[0] == "media"
    assert tools.audit.record_audit(query="q")[0] == "audit"
