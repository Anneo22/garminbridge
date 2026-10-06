// garmin-usb-watcher.swift — fire a command the instant a Garmin USB device attaches.
//
// Uses IOKit matching notifications (no polling). kIOFirstMatchNotification also
// fires for a device already present when the watcher starts, so a launchd restart
// while the watch is plugged in still triggers a sync. Garmin USB vendor id = 0x091e.
//
// Build:  swiftc -O garmin-usb-watcher.swift -o garmin-usb-watcher
// Run:    garmin-usb-watcher /path/to/export-voice-notes.sh [args...]
//         (everything after the binary is the command to run on attach)

import Foundation
import IOKit
import IOKit.usb

let GARMIN_VENDOR_ID = 0x091e

let args = Array(CommandLine.arguments.dropFirst())
guard !args.isEmpty else {
    FileHandle.standardError.write("usage: garmin-usb-watcher <script> [args...]\n".data(using: .utf8)!)
    exit(2)
}
let runArgs = args  // the command (script + its args) to execute on attach

// Coalesce bursts of match events (a device can present several interfaces) and let
// the device settle before running. Re-running is cheap (the importer dedupes).
var debounce: DispatchWorkItem?
func triggerRun() {
    debounce?.cancel()
    let work = DispatchWorkItem {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = runArgs
        do { try p.run() } catch {
            FileHandle.standardError.write("watcher: failed to run: \(error)\n".data(using: .utf8)!)
        }
    }
    debounce = work
    DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: work)
}

// Read a device's USB vendor id from the IORegistry.
func vendorID(of device: io_object_t) -> Int? {
    guard let cf = IORegistryEntrySearchCFProperty(device, kIOServicePlane, "idVendor" as CFString,
                                                   kCFAllocatorDefault, IOOptionBits(kIORegistryIterateRecursively)) else { return nil }
    return (cf as? NSNumber)?.intValue
}

// Drain the iterator (required to re-arm the notification). We match ALL USB host
// devices and filter by vendor id here, which is more reliable across macOS versions
// than putting idVendor in the matching dictionary.
func handleMatches(_ iterator: io_iterator_t) {
    var found = false
    var dev = IOIteratorNext(iterator)
    while dev != 0 {
        if vendorID(of: dev) == GARMIN_VENDOR_ID { found = true }
        IOObjectRelease(dev)
        dev = IOIteratorNext(iterator)
    }
    if found {
        FileHandle.standardError.write("watcher: Garmin attached -> scheduling sync\n".data(using: .utf8)!)
        triggerRun()
    }
}

let notifyPort = IONotificationPortCreate(kIOMainPortDefault)
let source = IONotificationPortGetRunLoopSource(notifyPort!).takeUnretainedValue()
CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .defaultMode)

// Match all USB host devices; we filter by vendor id in handleMatches().
let matching = IOServiceMatching("IOUSBHostDevice")

let callback: IOServiceMatchingCallback = { _, iterator in handleMatches(iterator) }
var iter: io_iterator_t = 0
let kr = IOServiceAddMatchingNotification(notifyPort, kIOFirstMatchNotification,
                                          matching, callback, nil, &iter)
guard kr == KERN_SUCCESS else {
    FileHandle.standardError.write("watcher: IOServiceAddMatchingNotification failed: \(kr)\n".data(using: .utf8)!)
    exit(1)
}
handleMatches(iter)   // arm the iterator + handle an already-connected watch

FileHandle.standardError.write("watcher: running (vendor 0x091e)\n".data(using: .utf8)!)
CFRunLoopRun()
