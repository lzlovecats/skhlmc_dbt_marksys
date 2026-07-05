from pathlib import Path

import streamlit.components.v1 as components


_speech_recorder_component = components.declare_component(
    "speech_recorder",
    path=str(Path(__file__).parent / "components" / "speech_recorder"),
)


def render_speech_recorder(key: str, bell_src: str = "", bell_schedule=None):
    return _speech_recorder_component(
        bell_src=bell_src,
        bell_schedule=bell_schedule or [],
        key=key,
        default=None,
    )
