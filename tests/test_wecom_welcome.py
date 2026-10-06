from app.messages import WELCOME_MESSAGE


def test_welcome_message_lists_main_capabilities() -> None:
    assert "您好，我是红财万富科技有限公司智能助手" in WELCOME_MESSAGE
    assert "1. 📘 查询公司制度和相关政策" in WELCOME_MESSAGE
    assert "2. 📦 查询产品信息，包括参数、价格、配件、图片和视频" in WELCOME_MESSAGE
    assert "3. 💬 根据客户情况，整理可以直接发送的回复话术" in WELCOME_MESSAGE
    assert WELCOME_MESSAGE.endswith("请直接输入您的问题，我来帮您查询。")
