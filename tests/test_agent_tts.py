from agent_clicker.desktop_agent.tts import TTSPlayer


def test_supertonic_language_selection_handles_english_and_swedish():
    assert TTSPlayer._language_for("All right, the task is finished.") == "en"
    assert TTSPlayer._language_for("Tack, jag är klar med ändringen.") == "sv"


def test_stream_chunker_releases_a_short_complete_sentence():
    player = TTSPlayer.__new__(TTSPlayer)
    player._stream_buffer = "All right, I've finished the task. More is arriving"

    chunks = player._take_ready_chunks()

    assert chunks == ["All right, I've finished the task."]
    assert player._stream_buffer == "More is arriving"


def test_full_reply_is_used_when_visual_stream_did_not_feed_tts():
    player = TTSPlayer.__new__(TTSPlayer)
    player._enabled = True
    player._stream_lock = __import__("threading").Lock()
    player._stream_buffer = ""
    player._stream_spoken = ""
    spoken = []
    player.speak = spoken.append

    player.end_stream("One complete reply without audio joins.")

    assert spoken == ["One complete reply without audio joins."]
