import SwiftUI

/// AppShell — native window chrome: traffic lights, vibrancy sidebar, content.
/// Ported from `ui_kits/mac-app/AppShell.jsx`.
struct RootView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        VStack(spacing: 0) {
            titleBar
            HStack(spacing: 0) {
                Sidebar()
                ZStack {
                    VXColor.surfaceApp
                    content
                }
            }
        }
        .background(VXColor.surfaceApp)
        .ignoresSafeArea()
    }

    private var titleBar: some View {
        // The window hides the native title bar but keeps macOS's real traffic-light
        // buttons (functional), which float at top-left. We do NOT draw our own —
        // we just reserve space for them with leading padding.
        HStack(spacing: 16) {
            Text(state.route == .editor ? (state.activeProject?.name ?? "VX") : "VX — AI Video Editor")
                .font(VXFont.sm).foregroundStyle(VXColor.textTertiary)
            Spacer()
            if !state.connected {
                Badge(tone: .danger, dot: true, text: "sidecar offline")
            }
        }
        .padding(.leading, 80)   // clear the real macOS window controls
        .padding(.trailing, 14)
        .frame(height: VXMetrics.titlebarH)
        .background(VXColor.materialChrome)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(VXColor.borderSubtle), alignment: .bottom)
    }

    @ViewBuilder private var content: some View {
        switch state.route {
        case .library: LibraryView()
        case .briefing: BriefingView()
        case .editor: EditorView()
        case .settings: SettingsView()
        }
    }
}

struct Sidebar: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Logo().padding(.horizontal, 8).padding(.bottom, 12).padding(.top, 2)
            NavItem(icon: "folder", label: "Library", active: state.route == .library) { state.route = .library }
            NavItem(icon: "sparkle", label: "Briefing", active: state.route == .briefing) { state.route = .briefing }
            NavItem(icon: "settings", label: "Settings", active: state.route == .settings) { state.route = .settings }

            Eyebrow("Recents").padding(.horizontal, 8).padding(.top, 18).padding(.bottom, 8)
            ForEach(state.projects.prefix(4)) { p in
                NavItem(icon: "film", label: p.name,
                        active: state.route == .editor && state.activeProject?.id == p.id) { state.open(p) }
            }
            Spacer()
            Text("\(state.activeProject?.provider.capitalized ?? "Gemini") · \(state.connected ? "connected" : "offline")")
                .font(VXFont.mono(11)).foregroundStyle(VXColor.textFaint).padding(8)
        }
        .padding(.horizontal, 10).padding(.vertical, 14)
        .frame(width: VXMetrics.railSidebar)
        .background(VXColor.materialChrome)
        .overlay(Rectangle().frame(width: 1).foregroundStyle(VXColor.borderSubtle), alignment: .trailing)
    }
}

struct Logo: View {
    var body: some View {
        HStack(spacing: 9) {
            ZStack {
                RoundedRectangle(cornerRadius: 7).fill(VXColor.accent).frame(width: 26, height: 26)
                Image(systemName: "play.fill").font(.system(size: 11)).foregroundStyle(VXColor.textOnAccent)
            }
            Text("VX").font(.system(size: 16, weight: .bold)).foregroundStyle(VXColor.textPrimary)
        }
    }
}

struct NavItem: View {
    let icon: String
    let label: String
    var active: Bool = false
    var badge: String? = nil
    var action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                VXIcon(name: icon, size: 16, color: active ? VXColor.accent : VXColor.textTertiary)
                Text(label).font(.system(size: 13, weight: active ? .semibold : .medium))
                    .foregroundStyle(active ? VXColor.accent : VXColor.textSecondary)
                    .lineLimit(1)
                Spacer()
                if let badge { Badge(tone: active ? .accent : .neutral, text: badge) }
            }
            .padding(.horizontal, 10).frame(height: 32)
            .background(active ? VXColor.accentSoft : (hovering ? VXColor.surfaceRaised : .clear))
            .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(VXMotion.fast, value: hovering)
    }
}
