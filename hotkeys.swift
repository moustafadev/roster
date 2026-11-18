// Global keyboard shortcuts for Roster (work from any app, no extra permission).
//   id 1 = Ctrl+Option+A    id 2 = Ctrl+Option+H
// If this file ever fails to build, install.sh falls back to hotkeys_stub.swift.

import Cocoa
import Carbon.HIToolbox

var huddleHotKeyAction: ((UInt32) -> Void)?
var huddleHotKeyRefs: [EventHotKeyRef?] = []

func installHotKeys(_ action: @escaping (UInt32) -> Void) {
    huddleHotKeyAction = action

    var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                             eventKind: UInt32(kEventHotKeyPressed))
    InstallEventHandler(GetApplicationEventTarget(), { (_, event, _) -> OSStatus in
        var hotKey = EventHotKeyID()
        let status = GetEventParameter(event,
                                       EventParamName(kEventParamDirectObject),
                                       EventParamType(typeEventHotKeyID),
                                       nil,
                                       MemoryLayout<EventHotKeyID>.size,
                                       nil,
                                       &hotKey)
        if status == noErr {
            let id = hotKey.id
            DispatchQueue.main.async {
                huddleHotKeyAction?(id)
            }
        }
        return noErr
    }, 1, &spec, nil, nil)

    let modifiers = UInt32(controlKey | optionKey)
    let keys: [(UInt32, UInt32)] = [(UInt32(kVK_ANSI_A), 1), (UInt32(kVK_ANSI_H), 2)]
    for (keyCode, id) in keys {
        var ref: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: OSType(0x48444C45), id: id)
        RegisterEventHotKey(keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &ref)
        huddleHotKeyRefs.append(ref)
    }
}
