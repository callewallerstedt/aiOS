#Requires AutoHotkey v2.0
#SingleInstance Force
InstallKeybdHook
#UseHook True

; Visible tray identity so it's obvious the macropad layer is alive.
A_IconTip := "aiOS Macropad"

^!+F12::Suspend
; Fresh launch should never stay suspended from a previous session.
Suspend(False)

global InsertDownAt := 0
global InsertHoldActive := false
global InsertLongFired := false
global DiscordMutedByUs := false
global WsaInited := false
global VoiceHoldMs := 280
global SeparateHoldToggleMs := 600   ; < this = toggle dictate; >= this = hold-to-talk
global VoiceSettingsCheckedAt := 0
global DiscordMuteEnabled := false
global DiscordMuteExplicit := ""
global DiscordMuteSendDelayMs := 35
global SeparateHotkeys := false
global VoiceHotkey := "Insert"          ; dictation key (or shared key in combined mode)
global AiosHotkey := "Insert"           ; open-aiOS key when SeparateHotkeys is true
global VoiceHotkeyRegistered := ""      ; fingerprint of last registration
global VoiceHotkeyDown := ""
global VoiceHotkeyUp := ""
global AiosHotkeyDown := ""
global DictationActive := false
global AiosToggleAt := 0
global DictateDownAt := 0

AiosLog(msg) {
    try FileAppend(FormatTime(, "HH:mm:ss") "  " msg "`n", A_ScriptDir "\.aios-ahk-log.txt", "UTF-8")
}

AiosHeartbeat() {
    path := A_ScriptDir "\.aios-ahk-heartbeat"
    try FileDelete(path)
    try FileAppend(A_NowUTC, path, "UTF-8")
}

; --- Combined mode: short tap opens aiOS, hold starts dictation ---

CombinedHotkeyDown(*) {
    global InsertHoldActive, InsertLongFired, VoiceHoldMs, VoiceHotkey
    if (InsertHoldActive) {
        return
    }
    InsertHoldActive := true
    InsertLongFired := false
    timeoutSec := Max(0.15, Min(0.80, VoiceHoldMs / 1000.0))
    if KeyWait(VoiceHotkey, "T" . timeoutSec) {
        InsertHoldActive := false
        AiosToggleNow()
        return
    }
    InsertLongFired := true
    VoiceStartFast()
    DiscordMuteStart(false)
    KeyWait(VoiceHotkey)
    VoiceStopFast()
    DiscordMuteStop()
    InsertHoldActive := false
    InsertLongFired := false
}

; --- Separate mode ---
; Quick press (< 0.6s): start dictation, leave it on until next press (toggle).
; Hold (>= 0.6s): hold-to-talk — stop as soon as the key is released.
;
; Macro Deck often only sends a short key pulse on press (logs showed 0ms hold).
; For hold-to-talk you MUST also fire something on Button Released:
;   - send the same dictate key again, OR
;   - run voice_ptt_up.bat
; Press action: dictate key or voice_ptt_down.bat

SeparateDictationDown(*) {
    global DictationActive, InsertHoldActive, VoiceHotkey, DictateDownAt, SeparateHoldToggleMs
    ; Already dictating: stop only after threshold (ignore Macro Deck quick-release echo).
    if (DictationActive) {
        elapsed := A_TickCount - DictateDownAt
        if (elapsed < SeparateHoldToggleMs) {
            AiosLog("dictate ignore echo at " elapsed "ms via " VoiceHotkey)
            return
        }
        AiosLog("dictate stop via " VoiceHotkey " after " elapsed "ms")
        SendToVoice("ptt_down")
        VoiceStopFast()
        DiscordMuteStop()
        DictationActive := false
        InsertHoldActive := false
        return
    }

    if (InsertHoldActive) {
        return
    }

    InsertHoldActive := true
    DictationActive := true
    DictateDownAt := A_TickCount
    AiosLog("dictate start via " VoiceHotkey)
    if !SendToVoice("ptt_down") {
        VoiceStartFast()
    }
    SetTimer(DeferDiscordMuteStart, -1)
}

SeparateDictationUp(*) {
    global DictationActive, InsertHoldActive, SeparateHoldToggleMs, DictateDownAt, VoiceHotkey
    heldMs := A_TickCount - DictateDownAt
    InsertHoldActive := false
    if (!DictationActive) {
        return
    }
    ; Tell the voice server (handles Macro Deck bats + real key-up holds).
    SendToVoice("ptt_up")
    if (heldMs >= SeparateHoldToggleMs) {
        AiosLog("dictate hold-release stop after " heldMs "ms via " VoiceHotkey)
        VoiceStopFast()
        DiscordMuteStop()
        DictationActive := false
    } else {
        AiosLog("dictate toggle-on (held " heldMs "ms) via " VoiceHotkey)
    }
}

DeferDiscordMuteStart() {
    DiscordMuteStart(false)
}

SeparateAiosDown(*) {
    global AiosHotkey, AiosToggleAt
    now := A_TickCount
    if (now - AiosToggleAt < 220) {
        return
    }
    AiosToggleAt := now
    AiosLog("aios toggle via " AiosHotkey)
    AiosToggleNow()
}

NormalizeVoiceHotkeyKey(keyName) {
    keyName := Trim(keyName)
    if (keyName = "") {
        return "Insert"
    }
    lower := StrLower(keyName)
    static aliases := Map(
        "pageup", "PgUp", "pgup", "PgUp",
        "pagedown", "PgDn", "pgdn", "PgDn",
        "del", "Delete", "ins", "Insert",
        "scrolllock", "ScrollLock",
        "appskey", "AppsKey", "menu", "AppsKey", "apps", "AppsKey",
        "mouse4", "XButton1", "mouse 4", "XButton1", "xbutton1", "XButton1",
        "mouse5", "XButton2", "mouse 5", "XButton2", "xbutton2", "XButton2",
    )
    if aliases.Has(lower) {
        return aliases[lower]
    }
    if RegExMatch(keyName, "i)^F(\d{1,2})$", &m) {
        return "F" . m[1]
    }
    static known := Map(
        "insert", "Insert", "home", "Home", "end", "End",
        "delete", "Delete", "pause", "Pause",
    )
    if known.Has(lower) {
        return known[lower]
    }
    return keyName
}

UnregisterAllVoiceHotkeys() {
    global VoiceHotkeyDown, VoiceHotkeyUp, AiosHotkeyDown
    if (VoiceHotkeyDown != "") {
        try Hotkey VoiceHotkeyDown, "Off"
    }
    if (VoiceHotkeyUp != "") {
        try Hotkey VoiceHotkeyUp, "Off"
    }
    if (AiosHotkeyDown != "") {
        try Hotkey AiosHotkeyDown, "Off"
    }
    VoiceHotkeyDown := ""
    VoiceHotkeyUp := ""
    AiosHotkeyDown := ""
}

BindHotkey(name, callback) {
    ; Prefer hook form ($). Fall back to plain name if that fails (some F13–F24 setups).
    try {
        Hotkey "$" . name, callback, "On"
        return "$" . name
    } catch {
    }
    try {
        Hotkey name, callback, "On"
        return name
    } catch as err {
        AiosLog("FAILED to bind " name ": " err.Message)
        return ""
    }
}

RegisterHotkeys() {
    global SeparateHotkeys, VoiceHotkey, AiosHotkey, VoiceHotkeyRegistered
    global VoiceHotkeyDown, VoiceHotkeyUp, AiosHotkeyDown

    voiceKey := NormalizeVoiceHotkeyKey(VoiceHotkey = "" ? "Insert" : VoiceHotkey)
    aiosKey := NormalizeVoiceHotkeyKey(AiosHotkey = "" ? "Insert" : AiosHotkey)
    VoiceHotkey := voiceKey
    AiosHotkey := aiosKey

    fingerprint := (SeparateHotkeys ? "sep|" : "comb|") . voiceKey . "|" . aiosKey . "|v4"
    if (VoiceHotkeyRegistered = fingerprint) {
        return
    }

    UnregisterAllVoiceHotkeys()
    VoiceHotkeyRegistered := ""

    ok := true
    if (SeparateHotkeys) {
        if (voiceKey = aiosKey) {
            VoiceHotkeyDown := BindHotkey(voiceKey, CombinedHotkeyDown)
            ok := (VoiceHotkeyDown != "")
        } else {
            VoiceHotkeyDown := BindHotkey(voiceKey, SeparateDictationDown)
            VoiceHotkeyUp := BindHotkey(voiceKey . " up", SeparateDictationUp)
            AiosHotkeyDown := BindHotkey(aiosKey, SeparateAiosDown)
            ok := (VoiceHotkeyDown != "" && VoiceHotkeyUp != "" && AiosHotkeyDown != "")
        }
    } else {
        VoiceHotkeyDown := BindHotkey(voiceKey, CombinedHotkeyDown)
        ok := (VoiceHotkeyDown != "")
    }

    if (ok) {
        VoiceHotkeyRegistered := fingerprint
        AiosLog("registered " fingerprint " down=" VoiceHotkeyDown " up=" VoiceHotkeyUp " aios=" AiosHotkeyDown)
    } else {
        AiosLog("registration incomplete for " fingerprint)
    }
}

LoadVoiceConfig() {
    global VoiceHoldMs, VoiceSettingsCheckedAt, DiscordMuteEnabled, DiscordMuteExplicit
    global VoiceHotkey, AiosHotkey, SeparateHotkeys
    now := A_TickCount
    if (now - VoiceSettingsCheckedAt < 1500) {
        return
    }
    VoiceSettingsCheckedAt := now
    path := A_ScriptDir "\helper_config.json"
    holdMs := 280
    discordEnabled := false
    discordHotkey := ""
    voiceHotkey := "Insert"
    aiosHotkey := "Insert"
    separate := false
    if FileExist(path) {
        try {
            txt := FileRead(path, "UTF-8")
            if RegExMatch(txt, '"hold_ms"\s*:\s*(\d+)', &m) {
                holdMs := Integer(m[1])
            } else if RegExMatch(txt, '"double_press_ms"\s*:\s*(\d+)', &m) {
                holdMs := Integer(m[1])
            }
            if RegExMatch(txt, '"discord_mute_enabled"\s*:\s*(true|false)', &m) {
                discordEnabled := (m[1] = "true")
            }
            if RegExMatch(txt, '"discord_mute_hotkey"\s*:\s*"([^"]*)"', &m) {
                discordHotkey := m[1]
            }
            if RegExMatch(txt, '"voice_hotkey"\s*:\s*"([^"]*)"', &m) {
                voiceHotkey := m[1]
            }
            if RegExMatch(txt, '"aios_hotkey"\s*:\s*"([^"]*)"', &m) {
                aiosHotkey := m[1]
            }
            if RegExMatch(txt, '"separate_hotkeys"\s*:\s*(true|false)', &m) {
                separate := (m[1] = "true")
            }
            ; Optional override: when Macro Deck owns mute, set ahk_discord_mute false.
            if RegExMatch(txt, '"ahk_discord_mute"\s*:\s*(true|false)', &m) {
                if (m[1] = "false") {
                    discordEnabled := false
                }
            } else if (separate) {
                ; Separate-key mode is driven by Macro Deck TCP actions which mute once.
                discordEnabled := false
            }
        } catch {
        }
    }
    if (holdMs < 150) {
        holdMs := 150
    }
    if (holdMs > 800) {
        holdMs := 800
    }
    VoiceHoldMs := holdMs
    DiscordMuteEnabled := discordEnabled
    DiscordMuteExplicit := HotkeyToExplicitSendSequence(discordHotkey)
    VoiceHotkey := NormalizeVoiceHotkeyKey(voiceHotkey = "" ? "Insert" : voiceHotkey)
    AiosHotkey := NormalizeVoiceHotkeyKey(aiosHotkey = "" ? "Insert" : aiosHotkey)
    SeparateHotkeys := separate
    RegisterHotkeys()
}

LoadDiscordMuteSettings() {
    global DiscordMuteEnabled, DiscordMuteExplicit
    path := A_ScriptDir "\helper_config.json"
    discordEnabled := false
    discordHotkey := ""
    if FileExist(path) {
        try {
            txt := FileRead(path, "UTF-8")
            if RegExMatch(txt, '"discord_mute_enabled"\s*:\s*(true|false)', &m) {
                discordEnabled := (m[1] = "true")
            }
            if RegExMatch(txt, '"discord_mute_hotkey"\s*:\s*"([^"]*)"', &m) {
                discordHotkey := m[1]
            }
        } catch {
        }
    }
    DiscordMuteEnabled := discordEnabled
    DiscordMuteExplicit := HotkeyToExplicitSendSequence(discordHotkey)
}

LoadVoiceHoldMs() {
    LoadVoiceConfig()
    return VoiceHoldMs
}

HotkeyToExplicitSendSequence(hotkey) {
    hotkey := NormalizeHotkeyInput(hotkey)
    if (hotkey = "") {
        return ""
    }
    parts := StrSplit(hotkey, "+")
    if (parts.Length = 0) {
        return ""
    }
    key := Trim(parts[parts.Length])
    down := ""
    up := ""
    if (parts.Length > 1) {
        Loop parts.Length - 1 {
            part := StrLower(Trim(parts[A_Index]))
            if (part = "ctrl" || part = "control") {
                down .= "{Ctrl down}"
                up := "{Ctrl up}" . up
            } else if (part = "shift") {
                down .= "{Shift down}"
                up := "{Shift up}" . up
            } else if (part = "alt" || part = "menu" || part = "altgr") {
                down .= "{Alt down}"
                up := "{Alt up}" . up
            } else if (part = "win" || part = "lwin" || part = "rwin" || part = "windows") {
                down .= "{LWin down}"
                up := "{LWin up}" . up
            }
        }
    }
    keyLower := StrLower(key)
    special := Map(
        "enter", "Enter", "return", "Enter", "space", "Space", "tab", "Tab",
        "esc", "Escape", "escape", "Escape", "backspace", "Backspace",
        "delete", "Delete", "del", "Delete", "insert", "Insert",
        "home", "Home", "end", "End", "pgup", "PgUp", "pgdn", "PgDn",
        "pageup", "PgUp", "pagedown", "PgDn",
    )
    keyName := ""
    if special.Has(keyLower) {
        keyName := special[keyLower]
    } else if RegExMatch(key, "i)^F(\d{1,2})$") {
        keyName := StrUpper(key)
    } else if (StrLen(key) = 1) {
        keyName := StrLower(key)
    } else {
        keyName := key
    }
    keySend := "{" . keyName . " down}{" . keyName . " up}"
    return down . keySend . up
}

SendDiscordHotkey(clearVoiceKey := true) {
    global DiscordMuteExplicit, DiscordMuteSendDelayMs, VoiceHotkey
    if (DiscordMuteExplicit = "") {
        return
    }
    if (clearVoiceKey) {
        ; The voice hotkey is still physically held during hold-to-dictate —
        ; release it virtually so Discord sees the mute chord cleanly.
        SendEvent("{" . VoiceHotkey . " up}")
        Sleep(DiscordMuteSendDelayMs)
    }
    SetKeyDelay(DiscordMuteSendDelayMs, DiscordMuteSendDelayMs)
    SendEvent(DiscordMuteExplicit)
    Sleep(DiscordMuteSendDelayMs)
}

NormalizeHotkeyInput(hotkey) {
    hotkey := Trim(hotkey)
    if (hotkey = "") {
        return ""
    }
    if InStr(hotkey, "+") {
        return RegExReplace(hotkey, "\s*\+\s*", "+")
    }
    return RegExReplace(hotkey, "\s+", "+")
}

DiscordMuteStart(clearVoiceKey := true) {
    global DiscordMutedByUs, DiscordMuteEnabled, DiscordMuteExplicit
    if (DiscordMutedByUs) {
        return
    }
    LoadDiscordMuteSettings()
    if (!DiscordMuteEnabled || DiscordMuteExplicit = "") {
        return
    }
    SendDiscordHotkey(clearVoiceKey)
    DiscordMutedByUs := true
}

DiscordMuteStop() {
    global DiscordMutedByUs, DiscordMuteEnabled, DiscordMuteExplicit
    if (!DiscordMutedByUs) {
        return
    }
    LoadDiscordMuteSettings()
    if (DiscordMuteEnabled && DiscordMuteExplicit != "") {
        SendDiscordHotkey(false)
    }
    DiscordMutedByUs := false
}

AiosToggleNow() {
    ; Fast path: talk to the already-running helper over TCP (same as voice).
    if (SendToHelper("toggle")) {
        return
    }
    pythonw := A_ScriptDir "\.venv\Scripts\pythonw.exe"
    if !FileExist(pythonw) {
        pythonw := "C:\Python313\pythonw.exe"
    }
    if !FileExist(pythonw) {
        pythonw := "pythonw.exe"
    }
    Run('"' pythonw '" "' A_ScriptDir '\helper_overlay.py" --toggle', A_ScriptDir)
}

SendToHelper(msg) {
    if !EnsureWSA() {
        return false
    }
    sock := DllCall("ws2_32\socket", "Int", 2, "Int", 1, "Int", 6, "UInt")
    if (sock = -1 || sock = 0xFFFFFFFF) {
        return false
    }
    addr := Buffer(16, 0)
    NumPut("UShort", 2, addr, 0)
    NumPut("UShort", DllCall("ws2_32\htons", "UShort", 48736, "UShort"), addr, 2)
    NumPut("UInt", 0x0100007F, addr, 4)   ; 127.0.0.1
    res := DllCall("ws2_32\connect", "Ptr", sock, "Ptr", addr.Ptr, "Int", 16, "Int")
    if (res != 0) {
        DllCall("ws2_32\closesocket", "Ptr", sock)
        return false
    }
    size := StrPut(msg, "UTF-8")
    payload := Buffer(size, 0)
    StrPut(msg, payload, "UTF-8")
    DllCall("ws2_32\send", "Ptr", sock, "Ptr", payload.Ptr, "Int", size - 1, "Int", 0, "Int")
    DllCall("ws2_32\closesocket", "Ptr", sock)
    return true
}

; Ctrl+Space is intentionally unbound here — Assetto Corsa uses it to reset VR view.

; --- fast TCP send to the voice server (no Python spawn) ---

EnsureWSA() {
    global WsaInited
    if (WsaInited) {
        return true
    }
    buf := Buffer(400, 0)
    if (DllCall("ws2_32\WSAStartup", "UShort", 0x202, "Ptr", buf.Ptr, "Int") != 0) {
        return false
    }
    WsaInited := true
    return true
}

SendToVoice(msg) {
    if !EnsureWSA() {
        return false
    }
    sock := DllCall("ws2_32\socket", "Int", 2, "Int", 1, "Int", 6, "UInt")
    if (sock = -1 || sock = 0xFFFFFFFF) {
        return false
    }
    addr := Buffer(16, 0)
    NumPut("UShort", 2, addr, 0)
    NumPut("UShort", DllCall("ws2_32\htons", "UShort", 48737, "UShort"), addr, 2)
    NumPut("UInt", 0x0100007F, addr, 4)   ; 127.0.0.1 in network byte order
    res := DllCall("ws2_32\connect", "Ptr", sock, "Ptr", addr.Ptr, "Int", 16, "Int")
    if (res != 0) {
        DllCall("ws2_32\closesocket", "Ptr", sock)
        return false
    }
    size := StrPut(msg, "UTF-8")
    payload := Buffer(size, 0)
    StrPut(msg, payload, "UTF-8")
    DllCall("ws2_32\send", "Ptr", sock, "Ptr", payload.Ptr, "Int", size - 1, "Int", 0, "Int")
    DllCall("ws2_32\closesocket", "Ptr", sock)
    return true
}

VoiceStartFast() {
    if (SendToVoice("start")) {
        return
    }
    pythonw := A_ScriptDir "\.venv\Scripts\pythonw.exe"
    if !FileExist(pythonw) {
        pythonw := "C:\Python313\pythonw.exe"
    }
    if !FileExist(pythonw) {
        pythonw := "pythonw.exe"
    }
    Run('"' pythonw '" "' A_ScriptDir '\voice_dictation.py" --start', A_ScriptDir, "Hide")
}

VoiceStopFast() {
    SendToVoice("stop")
}

; Initial config load registers the hotkey before the user does anything.
LoadVoiceConfig()
; Poll the config so Settings → Voice hotkey changes take effect within 2s
; even when the user hasn't pressed the current hotkey.
SetTimer(LoadVoiceConfig, 1700)
AiosHeartbeat()
SetTimer(AiosHeartbeat, 5000)

::constantyl::constantly
::missspell::misspell
::missspelled::misspelled
::missspelling::misspelling
::autocorect::autocorrect
::autocorret::autocorrect
::teh::the
::adn::and
::dont::don't
::cant::can't
::wont::won't
::im::I'm
::ive::I've
::ill::I'll
::intuaive::intuitive
::intuative::intuitive
::visulas::visuals
::becuase::because
::seperate::separate
::definately::definitely
::recieve::receive
::pelase::please
::wallersetdt::wallerstedt
::taht::that
::yuo::you
::@gm::calle.wallerstedt@gmail.com
::@cm::callew@chalmers.se
