// Roster — a small always-on-top panel that shows every running
// Claude Code session. The UI is widget.html; this file is only the window,
// the 1-second refresh, and the bridge for clicks coming from the page.
//
// Build:  swiftc -O main.swift hotkeys.swift -o Roster   (install.sh does this)

import Cocoa
import WebKit

let baseDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".roster")
let sessionsDir = baseDir.appendingPathComponent("sessions")
let panelWidth: CGFloat = 340

final class HuddlePanel: NSPanel {
    override var canBecomeKey: Bool { return true }
    override var canBecomeMain: Bool { return false }
}

final class HuddleWebView: WKWebView {
    // Let the first click on the (inactive) panel count as a real click.
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { return true }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler {
    var panel: HuddlePanel!
    var webView: HuddleWebView!
    var timer: Timer?
    var statusItem: NSStatusItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.createDirectory(at: sessionsDir, withIntermediateDirectories: true)

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "huddle")

        let frame = initialFrame(height: 56)
        webView = HuddleWebView(frame: NSRect(x: 0, y: 0, width: frame.width, height: frame.height),
                                configuration: config)
        webView.setValue(false, forKey: "drawsBackground")
        webView.autoresizingMask = [.width, .height]

        panel = HuddlePanel(contentRect: frame,
                            styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered,
                            defer: false)
        panel.isOpaque = false
        panel.backgroundColor = NSColor.clear
        panel.hasShadow = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isFloatingPanel = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.contentView = webView
        panel.orderFrontRegardless()

        setUpMenuBar()
        // Global shortcuts: Ctrl+Option+A = allow the waiting request, Ctrl+Option+H = show/hide.
        installHotKeys { [weak self] id in
            self?.hotKey(id)
        }

        let page = baseDir.appendingPathComponent("widget.html")
        webView.loadFileURL(page, allowingReadAccessTo: baseDir)

        timer = Timer.scheduledTimer(timeInterval: 1.0, target: self,
                                     selector: #selector(tick), userInfo: nil, repeats: true)
    }

    // Menu bar icon: shows how many sessions are running, and brings the panel back.
    func setUpMenuBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            if let image = NSImage(systemSymbolName: "person.3.fill", accessibilityDescription: "Roster") {
                button.image = image
                button.imagePosition = .imageLeading
            } else {
                button.title = "AH"
            }
        }
        let menu = NSMenu()
        let toggle = NSMenuItem(title: "Show / Hide Roster", action: #selector(togglePanel), keyEquivalent: "")
        toggle.target = self
        menu.addItem(toggle)
        menu.addItem(NSMenuItem.separator())
        let quit = NSMenuItem(title: "Quit Roster", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
    }

    @objc func togglePanel() {
        if panel.isVisible {
            panel.orderOut(nil)
        } else {
            panel.orderFrontRegardless()
        }
    }

    func hotKey(_ id: UInt32) {
        if id == 1 {
            webView.evaluateJavaScript("window.huddleHotkey && window.huddleHotkey('allow')",
                                       completionHandler: nil)
        } else if id == 2 {
            togglePanel()
        }
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }

    // Top-right corner of the main screen, or wherever it was last dragged to.
    func initialFrame(height: CGFloat) -> NSRect {
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        var x = visible.maxX - panelWidth - 16
        var top = visible.maxY - 16
        let defaults = UserDefaults.standard
        if defaults.object(forKey: "huddleX") != nil {
            let savedX = CGFloat(defaults.double(forKey: "huddleX"))
            let savedTop = CGFloat(defaults.double(forKey: "huddleTop"))
            let probe = NSPoint(x: savedX + 20, y: savedTop - 20)
            for screen in NSScreen.screens {
                if screen.frame.contains(probe) {
                    x = savedX
                    top = savedTop
                    break
                }
            }
        }
        return NSRect(x: x, y: top - height, width: panelWidth, height: height)
    }

    // Read every session file, drop the ones whose process is gone, hand the rest to the page.
    @objc func tick() {
        var sessions: [[String: Any]] = []
        let files = (try? FileManager.default.contentsOfDirectory(at: sessionsDir,
                                                                  includingPropertiesForKeys: nil)) ?? []
        for file in files {
            if file.pathExtension != "json" { continue }
            guard let data = try? Data(contentsOf: file) else { continue }
            guard let parsed = try? JSONSerialization.jsonObject(with: data) else { continue }
            guard let session = parsed as? [String: Any] else { continue }
            let pid = (session["pid"] as? NSNumber)?.int32Value ?? 0
            if pid > 1 && kill(pid, 0) != 0 && errno == ESRCH {
                try? FileManager.default.removeItem(at: file)
                continue
            }
            sessions.append(session)
        }
        var running = 0
        var waiting = false
        for session in sessions {
            if session["session_id"] == nil { continue }  // today's log, not a session
            running += 1
            if (session["status"] as? String) == "waiting" { waiting = true }
        }
        if let button = statusItem.button, button.image != nil {
            button.title = running == 0 ? "" : " \(running)" + (waiting ? " !" : "")
        }

        guard let out = try? JSONSerialization.data(withJSONObject: sessions) else { return }
        guard let json = String(data: out, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.huddleRender && window.huddleRender(" + json + ")",
                                   completionHandler: nil)
    }

    func number(_ value: Any?) -> CGFloat {
        return CGFloat((value as? NSNumber)?.doubleValue ?? 0)
    }

    func runHelper(_ arguments: [String]) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [baseDir.appendingPathComponent("hook.py").path] + arguments
        try? process.run()
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any] else { return }
        let type = body["type"] as? String ?? ""
        let id = body["id"] as? String ?? ""

        if type == "resize" {
            let height = max(40, min(number(body["height"]), 900))
            var frame = panel.frame
            let top = frame.maxY
            frame.size.height = height
            frame.origin.y = top - height
            panel.setFrame(frame, display: true)
            panel.invalidateShadow()
        } else if type == "move" {
            var frame = panel.frame
            frame.origin.x += number(body["dx"])
            frame.origin.y -= number(body["dy"])
            panel.setFrame(frame, display: true)
        } else if type == "moveEnd" {
            UserDefaults.standard.set(Double(panel.frame.minX), forKey: "huddleX")
            UserDefaults.standard.set(Double(panel.frame.maxY), forKey: "huddleTop")
        } else if type == "sound" {
            let name = body["name"] as? String ?? "Glass"
            NSSound(named: NSSound.Name(name))?.play()
        } else if type == "hide" {
            panel.orderOut(nil)
        } else if !type.isEmpty && !id.isEmpty {
            // focus, close, allow, always, deny, reply ... all handled by hook.py
            var arguments = [type, id]
            if let text = body["text"] as? String, !text.isEmpty {
                arguments.append(text)
            }
            runHelper(arguments)
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
