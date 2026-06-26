// garmin-voice-menubar.swift, GarminBridge menu-bar control + settings.
// Everything is adjustable here, including transcription (no Terminal needed). State lives
// in the config the control script reads/writes; its path comes from env GVE_CTL.
//
// Build:  swiftc -O garmin-voice-menubar.swift -o garmin-voice-menubar

import Cocoa

let CTL = ProcessInfo.processInfo.environment["GVE_CTL"] ?? "garmin-voice"
let BINDIR = (CTL as NSString).deletingLastPathComponent

@discardableResult
func ctl(_ args: [String]) -> String {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = [CTL] + args
    let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
    do { try p.run() } catch { return "" }
    p.waitUntilExit()
    return String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
}

func cfgGet(_ key: String) -> String { ctl(["get", key]).trimmingCharacters(in: .whitespacesAndNewlines) }
func keyVar(_ id: String) -> String { "GVE_\(id.uppercased())_KEY" }

func statusField(_ s: String, _ key: String) -> String {
    for line in s.split(separator: "\n") where line.contains(key) {
        if let r = line.range(of: ":") { return line[r.upperBound...].trimmingCharacters(in: .whitespaces) }
    }
    return ""
}

func notify(_ msg: String) {
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    p.arguments = ["-e", "display notification \"\(msg)\" with title \"GarminBridge\""]
    try? p.run()
}

// Run the transcription setup in the background. Cloud backends finish instantly; local
// backends install for a few minutes. The script posts its own notification when done, so
// the menu never blocks and no Terminal window opens.
func setupTranscription(_ args: [String]) {
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = ["\(BINDIR)/install-transcription.sh"] + args
    try? p.run()
}

// Native secure prompt for an API key; nil if cancelled or empty.
func askKey(_ provider: String) -> String? {
    NSApp.activate(ignoringOtherApps: true)
    let a = NSAlert()
    a.messageText = "\(provider) API key"
    a.informativeText = "Stored locally in your config (chmod 600), never uploaded anywhere else."
    a.addButton(withTitle: "Save"); a.addButton(withTitle: "Cancel")
    let tf = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
    a.accessoryView = tf
    a.window.initialFirstResponder = tf
    let r = a.runModal()
    let v = tf.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    return (r == .alertFirstButtonReturn && !v.isEmpty) ? v : nil
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var dest = ""

    // (label, id). Local backends install on-device; cloud backends need an API key.
    let transLocal = [("Parakeet (on-device, free)", "parakeet"), ("Whisper (on-device, free)", "whisper")]
    let transCloud = [("OpenAI", "openai"), ("Gemini", "gemini"), ("Groq", "groq"), ("Deepgram", "deepgram")]
    let cleanupProviders = [("OpenAI", "openai"), ("Groq", "groq"), ("Anthropic", "anthropic"), ("Gemini", "gemini")]

    func applicationDidFinishLaunching(_ n: Notification) {
        if let img = NSImage(systemSymbolName: "arrow.down.circle", accessibilityDescription: "GarminBridge") {
            item.button?.image = img
        } else {
            item.button?.title = "GB"   // guarantee a visible item even if the symbol is unavailable
        }
        menu.delegate = self
        item.menu = menu
    }

    func menuNeedsUpdate(_ m: NSMenu) {
        let s = ctl(["status"])
        let paused   = statusField(s, "auto-import").contains("PAUSED")
        let dmode    = deleteMode(s)
        let retDays  = retentionDays(s)
        let tBackend = parens(statusField(s, "transcription"))
        let cBackend = parens(statusField(s, "transcript cleanup"))
        dest = statusField(s, "destination")

        m.removeAllItems()
        m.addItem(disabled("GarminBridge"))
        m.addItem(disabled("  " + (paused ? "Paused" : "Active") + (dest.isEmpty ? "" : " · " + (dest as NSString).lastPathComponent)))
        m.addItem(.separator())
        m.addItem(action("Import voice notes now", #selector(importVoice)))
        m.addItem(action("Back up activities now", #selector(backupActivities)))
        m.addItem(action("Open voice memos folder", #selector(openFolder)))
        m.addItem(.separator())

        m.addItem(submenu("Remove from watch", [
            check("Keep on watch", dmode == "keep", #selector(setDeleteKeep)),
            check("After a verified copy", dmode == "now", #selector(setDeleteNow)),
            check("After it's transcribed", dmode == "transcribed", #selector(setDeleteTranscribed)),
        ]))
        m.addItem(submenu("Local audio", [
            check("Keep forever", retDays == "", #selector(setRetKeep)),
            check("Delete once transcribed", retDays == "0", #selector(setRet0)),
            check("Delete after 30 days", retDays == "30", #selector(setRet30)),
            check("Delete after 90 days", retDays == "90", #selector(setRet90)),
        ]))
        m.addItem(action("Change output folder…", #selector(changeRoot)))
        m.addItem(.separator())

        // Transcription, fully controllable here
        var tItems = [check("Off", tBackend.isEmpty, #selector(setTransOff))]
        for (label, id) in transLocal { tItems.append(pick(label, id, tBackend, #selector(chooseTrans(_:)))) }
        tItems.append(.separator())
        for (label, id) in transCloud { tItems.append(pick(label + " (API key)", id, tBackend, #selector(chooseTrans(_:)))) }
        tItems.append(.separator())
        var cItems = [check("Off", cBackend.isEmpty, #selector(setCleanupOff))]
        for (label, id) in cleanupProviders { cItems.append(pick(label, id, cBackend, #selector(chooseCleanup(_:)))) }
        tItems.append(submenu("Clean up transcripts (LLM)", cItems))
        m.addItem(submenu("Transcription" + (tBackend.isEmpty ? " (off)" : " · " + tBackend), tItems))
        m.addItem(.separator())

        m.addItem(action(paused ? "Resume auto-import" : "Pause (free the watch for other apps)",
                         paused ? #selector(resume) : #selector(pause)))
        m.addItem(action("Quit GarminBridge", #selector(quit)))
    }

    // status text helpers
    func deleteMode(_ s: String) -> String {
        let v = statusField(s, "delete-from-watch")
        if v.contains("transcript") { return "transcribed" }
        if v.contains("copy") { return "now" }
        return "keep"
    }
    func retentionDays(_ s: String) -> String {
        let v = statusField(s, "local retention")
        if v.hasPrefix("keep") { return "" }
        if let r = v.range(of: "after "), let d = v[r.upperBound...].split(separator: "d").first { return String(d) }
        return ""
    }
    func parens(_ v: String) -> String {   // "on (openai)" -> "openai"; "off" -> ""
        if v.hasPrefix("off") || v.isEmpty { return "" }
        if let a = v.range(of: "("), let b = v.range(of: ")") { return String(v[a.upperBound..<b.lowerBound]) }
        return ""
    }

    // menu builders
    func disabled(_ t: String) -> NSMenuItem { let i = NSMenuItem(title: t, action: nil, keyEquivalent: ""); i.isEnabled = false; return i }
    func action(_ t: String, _ sel: Selector) -> NSMenuItem { let i = NSMenuItem(title: t, action: sel, keyEquivalent: ""); i.target = self; return i }
    func check(_ t: String, _ on: Bool, _ sel: Selector) -> NSMenuItem { let i = action(t, sel); i.state = on ? .on : .off; return i }
    func pick(_ t: String, _ id: String, _ current: String, _ sel: Selector) -> NSMenuItem {
        let i = action(t, sel); i.representedObject = id; i.state = (current == id) ? .on : .off; return i
    }
    func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenuItem {
        let parent = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let sm = NSMenu(); items.forEach { sm.addItem($0) }; parent.submenu = sm; return parent
    }

    @objc func importVoice()      { DispatchQueue.global().async { ctl(["sync"]) } }
    @objc func backupActivities() { DispatchQueue.global().async { ctl(["activities"]) } }
    @objc func openFolder()       { if !dest.isEmpty { NSWorkspace.shared.open(URL(fileURLWithPath: dest)) } }
    @objc func pause()  { ctl(["pause"]) }
    @objc func resume() { ctl(["resume"]) }
    @objc func quit()   { NSApp.terminate(nil) }

    @objc func setDeleteKeep()        { ctl(["unset", "GARMIN_VOICE_DELETE"]) }
    @objc func setDeleteNow()         { ctl(["set", "GARMIN_VOICE_DELETE", "now"]) }
    @objc func setDeleteTranscribed() { ctl(["set", "GARMIN_VOICE_DELETE", "transcribed"]) }
    @objc func setRetKeep() { ctl(["unset", "GVE_AUDIO_RETENTION_DAYS"]) }
    @objc func setRet0()    { ctl(["set", "GVE_AUDIO_RETENTION_DAYS", "0"]) }
    @objc func setRet30()   { ctl(["set", "GVE_AUDIO_RETENTION_DAYS", "30"]) }
    @objc func setRet90()   { ctl(["set", "GVE_AUDIO_RETENTION_DAYS", "90"]) }

    @objc func setTransOff()   { ctl(["unset", "GVE_TRANSCRIBE"]) }
    @objc func setCleanupOff() { ctl(["unset", "GVE_TRANSCRIPT_CLEANUP"]) }

    @objc func chooseTrans(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        if id == "parakeet" || id == "whisper" {
            notify("Installing \(id) transcription in the background. You'll get a notification when it's ready.")
            setupTranscription(["--backend", id])
            return
        }
        // cloud: reuse an existing key, or ask for one
        if cfgGet(keyVar(id)).isEmpty {
            guard let k = askKey(sender.title.replacingOccurrences(of: " (API key)", with: "")) else { return }
            setupTranscription(["--backend", id, "--key", k])
        } else {
            setupTranscription(["--backend", id])
        }
    }

    @objc func chooseCleanup(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        if cfgGet(keyVar(id)).isEmpty {
            guard let k = askKey(sender.title) else { return }
            ctl(["set", keyVar(id), k])
        }
        ctl(["set", "GVE_CLEANUP_BACKEND", id])
        ctl(["set", "GVE_TRANSCRIPT_CLEANUP", "1"])
        notify("Transcript cleanup on (\(id)).")
    }

    // Pick the single output root (the "Garmin Bridge" folder that holds Voice Memo + Backups),
    // then offer to move existing files into it. The root is what the user controls; per-feature
    // paths derive from it.
    @objc func changeRoot() {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.canCreateDirectories = true
        panel.prompt = "Use as Garmin Bridge folder"
        panel.message = "Pick where GarminBridge keeps everything. Voice Memo and Backups live inside."
        guard panel.runModal() == .OK, var root = panel.url else { return }
        // keep the user's pick tidy: nest a "Garmin Bridge" folder unless they already chose one.
        if root.lastPathComponent != "Garmin Bridge" { root = root.appendingPathComponent("Garmin Bridge") }
        ctl(["root", root.path])
        let a = NSAlert()
        a.messageText = "Move existing files in?"
        a.informativeText = "Move your current voice memos and activity backups into \(root.lastPathComponent)? Existing files are never overwritten."
        a.addButton(withTitle: "Move them"); a.addButton(withTitle: "Leave them")
        if a.runModal() == .alertFirstButtonReturn {
            DispatchQueue.global().async { ctl(["migrate"]); notify("Moved your files into \(root.lastPathComponent).") }
        } else { notify("New files will go into \(root.lastPathComponent).") }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
