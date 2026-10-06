from pathlib import Path


STATIC_DIR = Path(__file__).parents[1] / "src" / "agentpms" / "api" / "static"


def test_conversation_keeps_composer_visible_while_messages_scroll() -> None:
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert "grid-template-rows: minmax(0, 1fr);" in styles
    assert ".conversation-panel {\n  min-width: 0;\n  min-height: 0;" in styles
    assert "flex: 1 1 0;" in styles
    assert "overflow-y: auto;" in styles
    assert "flex: 0 0 auto;" in styles


def test_small_screens_bound_the_conversation_to_the_viewport() -> None:
    styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert ".conversation-panel { height: calc(100vh - 118px); height: calc(100dvh - 118px);" in styles
    assert ".conversation-panel { height: calc(100vh - 68px); height: calc(100dvh - 68px);" in styles


def test_only_the_latest_confirmation_remains_actionable() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "closeConfirmationCards" in script
    assert "confirmation-closed" in script
    assert "awaitingConfirmation" in script
    assert "response.request_id" in script
