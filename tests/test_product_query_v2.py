"""Product-query V2 contract and isolated PostgreSQL execution tests."""

from app.business.product_query import (
    ProductFieldRequest,
    ProductFilter,
    ProductQuery,
    ProductReference,
    validate_product_query,
)
from app.business.service import BusinessAgent
from app.business.store import BusinessStore


def test_validation_rejects_ungrounded_fields_and_detects_real_ambiguity() -> None:
    proposed = ProductQuery(
        references=(ProductReference("LY0653", "fragment", "LY0653"),),
        requested_fields=(
            ProductFieldRequest("material", "材质"),
            ProductFieldRequest("components", "材质"),
        ),
    )

    validated = validate_product_query(
        proposed,
        "LY0653套装材质",
        can_view_cost=False,
    )

    assert validated.query.fields == ("material",)
    assert validated.removed_fields == ("components",)
    assert validated.ambiguities[0].code == "set_material_or_components"


def test_validation_never_expands_regular_price_to_cost() -> None:
    proposed = ProductQuery(
        requested_fields=(
            ProductFieldRequest("set_price", "价格"),
            ProductFieldRequest("cost_price", "价格"),
        )
    )

    validated = validate_product_query(proposed, "价格多少", can_view_cost=True)

    assert validated.query.fields == ("set_price",)
    assert validated.removed_fields == ("cost_price",)


def test_v2_sql_filters_more_than_500_published_products(isolated_database_url) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    with store._connect() as connection:
        connection.execute(
            """INSERT INTO product_records
            (id,sequence_number,model,status,published_snapshot)
            SELECT ('00000000-0000-0000-0000-' || lpad(value::text,12,'0'))::uuid,
            value,'BULK-' || lpad(value::text,4,'0'),'active',
            jsonb_build_object(
                'sequence_number',value,
                'model','BULK-' || lpad(value::text,4,'0'),
                'special_function',CASE WHEN value % 2 = 0 THEN '恒温' ELSE '' END
            )
            FROM generate_series(1,520) AS value"""
        )

    result = store.query_products_v2(
        ProductQuery(
            operation="list",
            filters=(
                ProductFilter(
                    "special_function",
                    "equals",
                    "恒温",
                    "恒温",
                    "fallback",
                ),
            ),
            page=52,
            page_size=5,
        ),
        include_sensitive=False,
    )

    assert result.total == 260
    assert [item["model"] for item in result.items] == [
        "BULK-0512",
        "BULK-0514",
        "BULK-0516",
        "BULK-0518",
        "BULK-0520",
    ]


def test_v2_model_fragment_is_parameterized_and_cost_is_not_projected(
    isolated_database_url,
) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    for model in ("LY0653GDT-T", "LY0653NBT", "OTHER-001"):
        record = store.create_product_record({"model": model, "cost_price": 66})
        store.set_product_status(record["id"], "active")

    matched = store.query_products_v2(
        ProductQuery(references=(ProductReference("0653", "fragment", "0653"),)),
        include_sensitive=False,
    )
    injected = store.query_products_v2(
        ProductQuery(
            references=(ProductReference("' OR TRUE --", "fragment", "' OR TRUE --"),)
        ),
        include_sensitive=False,
    )

    assert matched.total == 2
    assert all(item["cost_price"] is None for item in matched.items)
    assert injected.total == 0


def test_v2_database_supports_the_operator_matrix(isolated_database_url) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    for values in (
        {"model": "OP-A", "material": "全铜", "set_price": 100},
        {"model": "OP-B", "material": "不锈钢", "set_price": 200},
        {"model": "OP-C", "material": "", "set_price": None},
    ):
        record = store.create_product_record(values)
        store.set_product_status(record["id"], "active")

    cases = (
        (ProductFilter("material", "contains", "铜", "材质", "fallback"), {"OP-A"}),
        (ProductFilter("material", "is_present", "", "材质", "fallback"), {"OP-A", "OP-B"}),
        (ProductFilter("material", "is_empty", "", "材质", "fallback"), {"OP-C"}),
        (ProductFilter("set_price", "less_than", 200, "套装价", "fallback"), {"OP-A"}),
        (
            ProductFilter(
                "set_price", "less_than_or_equal", 200, "套装价", "fallback"
            ),
            {"OP-A", "OP-B"},
        ),
        (ProductFilter("set_price", "greater_than", 100, "套装价", "fallback"), {"OP-B"}),
        (
            ProductFilter(
                "set_price", "greater_than_or_equal", 200, "套装价", "fallback"
            ),
            {"OP-B"},
        ),
        (
            ProductFilter("set_price", "between", (90, 110), "套装价", "fallback"),
            {"OP-A"},
        ),
        (
            ProductFilter("model", "in", ("OP-A", "OP-C"), "型号", "fallback"),
            {"OP-A", "OP-C"},
        ),
    )
    for product_filter, expected in cases:
        result = store.query_products_v2(
            ProductQuery(operation="list", filters=(product_filter,)),
            include_sensitive=False,
        )
        assert {str(item["model"]) for item in result.items} == expected


def test_v2_ambiguity_follow_up_uses_structured_context_only(
    isolated_database_url,
) -> None:
    store = BusinessStore(isolated_database_url)
    store.initialize()
    record = store.create_product_record(
        {
            "model": "LY0653GDT-T",
            "material": "全铜",
            "components": [{"name": "顶喷"}, {"name": "手喷"}],
        }
    )
    store.set_product_status(record["id"], "active")
    agent = BusinessAgent(store)

    material = agent._answer_product_v2(
        "employee-1",
        "LY0653GDT-T主体材料",
        ProductQuery(
            references=(
                ProductReference("LY0653GDT-T", "exact", "LY0653GDT-T"),
            ),
            requested_fields=(ProductFieldRequest("material", "主体材料"),),
        ),
        can_view_cost=False,
        cost_access_message="无权限",
    )
    ambiguity = agent._answer_product_v2(
        "employee-1",
        "LY0653GDT-T套装材料",
        ProductQuery(
            references=(
                ProductReference("LY0653GDT-T", "exact", "LY0653GDT-T"),
            ),
            requested_fields=(ProductFieldRequest("material", "套装材料"),),
        ),
        can_view_cost=False,
        cost_access_message="无权限",
    )
    components = agent._answer_product_v2(
        "employee-1",
        "配件",
        ProductQuery(context_mode="continuation"),
        can_view_cost=False,
        cost_access_message="无权限",
    )

    assert material.content == "材质：全铜"
    assert "主体的材质，还是套装包含的配件" in ambiguity.content
    assert "顶喷" in components.content
    assert "材质：全铜" not in components.content
