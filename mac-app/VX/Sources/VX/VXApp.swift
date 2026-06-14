import SwiftUI
import AppKit

@main
struct VXApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var state = AppState()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(state)
                .frame(minWidth: 1080, minHeight: 680)
                .task { await state.bootstrap() }
                .preferredColorScheme(.dark)
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentMinSize)
    }
}

/// A bare SwiftUI executable launched via `swift run` has no app bundle, so
/// macOS gives it a background activation policy — the process runs but no
/// window shows and it never comes to the foreground. Promoting it to a
/// `.regular` app and activating fixes that. (A proper `.app` built in Xcode
/// gets this for free.)
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        SidecarManager.shared.startIfNeeded()
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}
