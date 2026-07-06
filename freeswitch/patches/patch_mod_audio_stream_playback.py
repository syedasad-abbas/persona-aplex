#!/usr/bin/env python3
"""Patch community mod_audio_stream to inject streamAudio into write frames.

The upstream community module decodes streamAudio responses into temp files and
emits mod_audio_stream::play events, but it does not write that audio into the
call. This patch adds a small PCM queue and drains it from WRITE_REPLACE frames.
"""

from pathlib import Path
import re
import sys


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/mod_audio_stream")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    if old not in text:
        raise RuntimeError(f"pattern not found in {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1))


def patch_header() -> None:
    path = ROOT / "mod_audio_stream.h"
    text = path.read_text()
    text = re.sub(
        r"(\s*switch_buffer_t \*sbuffer;\s*\n)(\s*int rtp_packets;)",
        r"\1"
        r"    switch_buffer_t *playback_buffer;"
        "\n\n"
        r"    switch_mutex_t *playback_mutex;"
        "\n\n"
        r"    uint32_t playback_queue_events;"
        "\n\n"
        r"    uint32_t playback_drain_events;"
        "\n\n"
        r"\2",
        text,
        count=1,
    )
    path.write_text(text)


def patch_glue() -> None:
    path = ROOT / "audio_streamer_glue.cpp"
    text = path.read_text()

    helper = r'''
    bool queuePlaybackAudio(const std::string& decoded, ProcessResult& out) {
        if (decoded.empty()) {
            return true;
        }

        switch_core_session_t* psession = switch_core_session_locate(m_sessionId.c_str());
        if (!psession) {
            push_err(out, m_sessionId, "processMessage - session not found while queuing playback");
            return false;
        }

        bool queued = false;
        auto *bug = get_media_bug(psession);
        if (!bug) {
            push_err(out, m_sessionId, "processMessage - no media bug while queuing playback");
        } else {
            auto* tech_pvt = (private_t*) switch_core_media_bug_get_user_data(bug);
            if (!tech_pvt || !tech_pvt->playback_buffer || !tech_pvt->playback_mutex) {
                push_err(out, m_sessionId, "processMessage - playback queue is not initialized");
            } else {
                switch_mutex_lock(tech_pvt->playback_mutex);
                switch_size_t free_space = switch_buffer_freespace(tech_pvt->playback_buffer);
                if (free_space < decoded.size()) {
                    switch_size_t inuse = switch_buffer_inuse(tech_pvt->playback_buffer);
                    switch_size_t need = (switch_size_t)decoded.size() - free_space;
                    if (need >= inuse) {
                        switch_buffer_zero(tech_pvt->playback_buffer);
                    } else {
                        switch_buffer_toss(tech_pvt->playback_buffer, need);
                    }
                }
                queued = switch_buffer_write(tech_pvt->playback_buffer, decoded.data(), decoded.size()) > 0;
                if (queued) {
                    tech_pvt->playback_queue_events++;
                    if (tech_pvt->playback_queue_events <= 5 || tech_pvt->playback_queue_events % 25 == 0) {
                        switch_log_printf(
                            SWITCH_CHANNEL_SESSION_LOG(psession),
                            SWITCH_LOG_INFO,
                            "mod_audio_stream: queued streamAudio playback event=%u bytes=%zu inuse=%zu\\n",
                            tech_pvt->playback_queue_events,
                            decoded.size(),
                            switch_buffer_inuse(tech_pvt->playback_buffer)
                        );
                    }
                }
                switch_mutex_unlock(tech_pvt->playback_mutex);
                if (!queued) {
                    push_err(out, m_sessionId, "processMessage - failed writing playback queue");
                }
            }
        }

        switch_core_session_rwunlock(psession);
        return queued;
    }
'''

    anchor = re.search(r"(\n\s*inline void send_initial_metadata\(switch_core_session_t \*session\) \{)", text)
    if not anchor:
        raise RuntimeError("could not find send_initial_metadata anchor")
    text = text[:anchor.start()] + "\n" + helper + text[anchor.start():]

    text = text.replace(
        "    // reserve file index\n",
        "        if (!queuePlaybackAudio(decoded, out)) {\n"
        "            return out;\n"
        "        }\n"
        "\n"
        "        // reserve file index\n",
        1,
    )

    init_code = (
        "        if (switch_mutex_init(&tech_pvt->playback_mutex, SWITCH_MUTEX_NESTED, pool) != SWITCH_STATUS_SUCCESS) {\n"
        "            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,\n"
        "                \"%s: Error creating playback mutex.\\n\", tech_pvt->sessionId);\n"
        "            return SWITCH_STATUS_FALSE;\n"
        "        }\n"
        "\n"
        "        if (switch_buffer_create_dynamic(&tech_pvt->playback_buffer, 32000, 32000, desiredSampling * channels * 2 * 30) != SWITCH_STATUS_SUCCESS) {\n"
        "            switch_log_printf(SWITCH_CHANNEL_SESSION_LOG(session), SWITCH_LOG_ERROR,\n"
        "                \"%s: Error creating playback buffer.\\n\", tech_pvt->sessionId);\n"
        "            return SWITCH_STATUS_FALSE;\n"
        "        }\n"
    )
    text, count = re.subn(
        r"(        if \(switch_buffer_create\(pool, &tech_pvt->sbuffer, buflen\) != SWITCH_STATUS_SUCCESS\) \{\n"
        r"            switch_log_printf\(SWITCH_CHANNEL_SESSION_LOG\(session\), SWITCH_LOG_ERROR,\n"
        r"                \"%s: Error creating switch buffer\.\\n\", tech_pvt->sessionId\);\n"
        r"            return SWITCH_STATUS_FALSE;\n"
        r"        \}\n)",
        lambda m: m.group(1) + "\n" + init_code,
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("could not insert playback buffer initialization")

    destroy_insert = (
        "        if (tech_pvt->playback_buffer) {\n"
        "            switch_buffer_destroy(&tech_pvt->playback_buffer);\n"
        "            tech_pvt->playback_buffer = nullptr;\n"
        "        }\n"
        "        if (tech_pvt->playback_mutex) {\n"
        "            switch_mutex_destroy(tech_pvt->playback_mutex);\n"
        "            tech_pvt->playback_mutex = nullptr;\n"
        "        }\n"
        "\n"
        "\\1"
    )
    text, count = re.subn(
        r"(        if \(tech_pvt->mutex\) \{\n"
        r"            switch_mutex_destroy\(tech_pvt->mutex\);\n"
        r"            tech_pvt->mutex = nullptr;\n"
        r"        \}\n)",
        destroy_insert,
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("could not insert playback buffer cleanup")

    path.write_text(text)


def patch_module() -> None:
    path = ROOT / "mod_audio_stream.c"
    text = path.read_text()

    write_replace = r'''
    case SWITCH_ABC_TYPE_WRITE_REPLACE:
    {
        switch_frame_t *frame = switch_core_media_bug_get_write_replace_frame(bug);
        if (frame && frame->data && frame->datalen && tech_pvt->playback_buffer && tech_pvt->playback_mutex) {
            switch_mutex_lock(tech_pvt->playback_mutex);
            if (switch_buffer_inuse(tech_pvt->playback_buffer) > 0) {
                switch_size_t wanted = frame->datalen;
                switch_size_t available = switch_buffer_inuse(tech_pvt->playback_buffer);
                switch_size_t to_read = available < wanted ? available : wanted;
                memset(frame->data, 0, frame->datalen);
                switch_buffer_read(tech_pvt->playback_buffer, frame->data, to_read);
                tech_pvt->playback_drain_events++;
                if (tech_pvt->playback_drain_events <= 5 || tech_pvt->playback_drain_events % 50 == 0) {
                    switch_log_printf(
                        SWITCH_CHANNEL_SESSION_LOG(session),
                        SWITCH_LOG_INFO,
                        "mod_audio_stream: drained streamAudio playback event=%u bytes=%zu remaining=%zu frame_bytes=%u\\n",
                        tech_pvt->playback_drain_events,
                        to_read,
                        switch_buffer_inuse(tech_pvt->playback_buffer),
                        frame->datalen
                    );
                }
                switch_core_media_bug_set_write_replace_frame(bug, frame);
            }
            switch_mutex_unlock(tech_pvt->playback_mutex);
        }
    }
    break;

'''

    text = re.sub(
        r"(\s*)case SWITCH_ABC_TYPE_WRITE:\n",
        lambda m: "\n" + write_replace + m.group(0),
        text,
        count=1,
    )

    text = text.replace(
        "switch_media_bug_flag_t flags = SMBF_READ_STREAM;",
        "switch_media_bug_flag_t flags = SMBF_READ_STREAM | SMBF_WRITE_REPLACE | SMBF_NO_PAUSE;",
        1,
    )

    path.write_text(text)


def main() -> None:
    patch_header()
    patch_glue()
    patch_module()


if __name__ == "__main__":
    main()
