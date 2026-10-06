from app.business.response_style import format_key_value_lines, normalize_wecom_answer


def test_format_key_value_lines_wraps_long_records_for_mobile() -> None:
    lines = format_key_value_lines(
        [
            ("价格", "170.00元"),
            ("库存", "32件"),
            ("颜色", "灰色"),
            ("材质", "全铝"),
            ("状态", "在售"),
        ]
    )

    assert lines == [
        "价格：170.00元｜库存：32件｜颜色：灰色｜材质：全铝",
        "状态：在售",
    ]


def test_normalize_wecom_answer_converts_markdown_table_to_record_blocks() -> None:
    content = """产品对比

| 型号 | 价格 | 库存 |
| --- | ---: | :--- |
| LTA-822 | 170.00元 | 32件 |
| LTA-830 | 198.00元 | 18件 |

以上为当前资料。"""

    normalized = normalize_wecom_answer(content)

    assert "| ---" not in normalized
    assert "1. 型号：LTA-822" in normalized
    assert "价格：170.00元｜库存：32件" in normalized
    assert "2. 型号：LTA-830" in normalized
    assert normalized.endswith("以上为当前资料。")


def test_normalize_wecom_answer_leaves_non_table_content_unchanged() -> None:
    content = "结论：可发货\n价格：170.00元｜库存：32件"

    assert normalize_wecom_answer(content) == content
