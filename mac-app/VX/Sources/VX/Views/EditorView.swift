import SwiftUI
import AVFoundation

/// The core workspace — the replacement for the one-shot HTML preview. Player +
/// timeline strip + EDL + status bar + inspector. Ported from
/// `ui_kits/mac-app/EditorView.jsx`, wired to the live storyboard.
struct EditorView: View {
    @EnvironmentObject var state: AppState

    private var sb: Storyboard? { state.storyboard }
    private var selected: Segment? {
        guard let sb, let idx = state.selectedSegment else { return sb?.segments.first }
        return sb.segments.first { $0.index == idx } ?? sb.segments.first
    }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            if sb == nil {
                emptyState
            } else {
                HStack(spacing: 0) {
                    if state.editMode == "timeline" { SectionRail() }
                    centerColumn
                    Inspector(segment: selected)
                        .frame(width: VXMetrics.railInspector)
                        .background(VXColor.surfacePanel)
                        .overlay(Rectangle().frame(width: 1).foregroundStyle(VXColor.borderDefault), alignment: .leading)
                }
            }
        }
    }

    // -- Toolbar -------------------------------------------------------------
    private var toolbar: some View {
        VXToolbar(left: {
            HStack(spacing: 12) {
                Text(sb?.title ?? state.activeProject?.name ?? "—")
                    .font(VXFont.heading).foregroundStyle(VXColor.textPrimary).lineLimit(1)
                if let v = state.activeProject?.latestVersion { Badge(tone: .accent, text: "v\(v)") }
                Badge(tone: .neutral, text: "\(state.activeProject?.clipCount ?? 0) clips")
            }
        }, center: {
            VXSegmentedControl(
                options: [.init(value: "story", label: "Story"), .init(value: "timeline", label: "Timeline")],
                selection: $state.editMode)
        }, right: {
            HStack(spacing: 8) {
                VXIconButton(icon: "download", label: "Export FCPXML")
                VXButton(title: "Preview", variant: .secondary) { state.runCut(proxyMode: true) }
                VXButton(title: "Render cut", variant: .primary, icon: "film") { state.runCut(proxyMode: false) }
            }
        })
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            Spacer()
            VXIcon(name: "wand", size: 30, color: VXColor.textMuted)
            Text("No storyboard yet").font(VXFont.heading).foregroundStyle(VXColor.textSecondary)
            Text("Run analysis to let VX review every clip and assemble a first cut.")
                .font(VXFont.sm).foregroundStyle(VXColor.textMuted)
            VXButton(title: "Analyze footage", variant: .primary, icon: "sparkle") {
                state.runAnalyze(timeline: state.editMode == "timeline", visual: false)
            }
            if let job = state.currentJob, !job.isTerminal {
                JobBar(job: job).frame(maxWidth: 420)
            }
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // -- Center column -------------------------------------------------------
    private var centerColumn: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if let seg = selected { Player(segment: seg, project: state.activeProject?.id ?? "") }
                    TimelineStrip()
                    Eyebrow("Edit Decision List").padding(.top, 22).padding(.bottom, 2)
                    EDLTable()
                }
                .padding(.horizontal, 22).padding(.vertical, 18)
            }
            statusBar
        }
        .frame(maxWidth: .infinity)
    }

    private var statusBar: some View {
        HStack(spacing: 16) {
            if let job = state.currentJob, !job.isTerminal {
                ProgressView().controlSize(.small)
                Text(job.stage ?? job.status).font(VXFont.mono(11)).foregroundStyle(VXColor.textBody)
            } else {
                Text("\(sb?.segments.count ?? 0) segments").font(VXFont.mono(11)).foregroundStyle(VXColor.textMuted)
                Text("~\(Timecode.fmt(sb?.totalDuration ?? 0))").font(VXFont.mono(11)).foregroundStyle(VXColor.textMuted)
            }
            Text("·").foregroundStyle(VXColor.textFaint)
            Text(costLine).font(VXFont.mono(11)).foregroundStyle(VXColor.textMuted)
            Spacer()
            Badge(tone: .success, dot: true, text: state.connected ? "connected" : "offline")
        }
        .padding(.horizontal, 22).frame(height: 36)
        .background(VXColor.surfacePanel)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(VXColor.borderDefault), alignment: .top)
    }

    private var costLine: String {
        let c = state.cost
        return "\(c.calls) calls · \(c.totalTokens.formatted()) tok · ~$\(String(format: "%.4f", c.estimatedCostUSD))"
    }
}

// ---------------------------------------------------------------- Player
struct Player: View {
    let segment: Segment
    let project: String
    @State private var player: AVPlayer?

    var body: some View {
        let hue = segment.purposeKind.color
        ZStack {
            RadialGradient(colors: [hue.opacity(0.18), Color(hex: 0x060606)], center: .top, startRadius: 0, endRadius: 480)
            if let player {
                PlayerLayerView(player: player)
            } else {
                VXIcon(name: "film", size: 28, color: .white.opacity(0.4))
            }
            VStack {
                HStack(spacing: 8) {
                    PurposeTag(purpose: segment.purpose)
                    Text(segment.clipID).font(VXFont.mono(11)).foregroundStyle(.white.opacity(0.7))
                    Spacer()
                }
                Spacer()
                HStack {
                    Timecode(range: (segment.inSec, segment.outSec), fontSize: 12).foregroundStyle(.white)
                    Spacer()
                    Text("proxy preview").font(VXFont.mono(11)).foregroundStyle(.white.opacity(0.55))
                }
            }
            .padding(14)
        }
        .aspectRatio(16.0/9.0, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusLG))
        .overlay(RoundedRectangle(cornerRadius: VXMetrics.radiusLG).stroke(VXColor.borderDefault, lineWidth: 1))
        .task(id: segment.clipID) { await loadProxy() }
    }

    private func loadProxy() async {
        guard !project.isEmpty else { return }
        let url = await APIClient.shared.proxyURL(project: project, clip: segment.clipID)
        player = AVPlayer(url: url)
    }
}

// ---------------------------------------------------------------- Timeline strip
struct TimelineStrip: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        let segs = state.storyboard?.segments ?? []
        let total = max(state.storyboard?.totalDuration ?? 1, 0.001)
        GeometryReader { geo in
            HStack(spacing: 1) {
                ForEach(segs) { s in
                    let active = state.selectedSegment == s.index
                    Rectangle()
                        .fill(s.purposeKind.color.opacity(active ? 1 : 0.82))
                        .frame(width: max(16, geo.size.width * (s.duration / total)))
                        .overlay(active ? RoundedRectangle(cornerRadius: 0).stroke(.white, lineWidth: 2) : nil)
                        .overlay(Text("\(s.index)").font(.system(size: 10, weight: .bold)).foregroundStyle(.white.opacity(0.9)))
                        .onTapGesture { state.selectedSegment = s.index }
                }
            }
        }
        .frame(height: 40)
        .clipShape(RoundedRectangle(cornerRadius: VXMetrics.radiusMD))
        .padding(.top, 14)
    }
}

// ---------------------------------------------------------------- EDL table
struct EDLTable: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        let segs = state.storyboard?.segments ?? []
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                cell("#", 28); cell("Clip", 150); cell("In → Out", 120)
                cell("Dur", 56); cell("Purpose", 110); cell("Description", nil)
            }
            .padding(.vertical, 8)
            .overlay(Rectangle().frame(height: 1).foregroundStyle(VXColor.borderDefault), alignment: .bottom)

            ForEach(segs) { s in
                let active = state.selectedSegment == s.index
                HStack(spacing: 0) {
                    Text("\(s.index)").font(VXFont.mono(12)).foregroundStyle(VXColor.textFaint).frame(width: 28, alignment: .leading)
                    Text(s.clipID).font(.system(size: 12, weight: .semibold)).foregroundStyle(VXColor.textBody).frame(width: 150, alignment: .leading).lineLimit(1)
                    Timecode(range: (s.inSec, s.outSec)).frame(width: 120, alignment: .leading)
                    Text("\(String(format: "%.1f", s.duration))s").font(VXFont.mono(12)).foregroundStyle(VXColor.textTertiary).frame(width: 56, alignment: .leading)
                    PurposeTag(purpose: s.purpose, size: .sm).frame(width: 110, alignment: .leading)
                    Text(s.description).font(VXFont.sm).foregroundStyle(VXColor.textMuted).frame(maxWidth: .infinity, alignment: .leading).lineLimit(1)
                }
                .padding(.vertical, 9)
                .background(active ? VXColor.accentSoft : .clear)
                .overlay(Rectangle().frame(height: 1).foregroundStyle(VXColor.borderSubtle), alignment: .bottom)
                .contentShape(Rectangle())
                .onTapGesture { state.selectedSegment = s.index }
            }
        }
    }

    private func cell(_ t: String, _ w: CGFloat?) -> some View {
        Text(t.uppercased()).font(.system(size: 10, weight: .semibold)).tracking(0.5)
            .foregroundStyle(VXColor.textFaint)
            .frame(width: w, alignment: .leading)
            .frame(maxWidth: w == nil ? .infinity : nil, alignment: .leading)
    }
}

// ---------------------------------------------------------------- Section rail
struct SectionRail: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        // Timeline mode groups segments by story-arc section.
        let arc = state.storyboard?.storyArc ?? []
        ScrollView {
            VStack(alignment: .leading, spacing: 2) {
                Eyebrow("Sections · \(arc.count)").padding(.horizontal, 6).padding(.bottom, 10)
                ForEach(Array(arc.enumerated()), id: \.offset) { _, sec in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(sec.title ?? "Section").font(.system(size: 12, weight: .medium)).foregroundStyle(VXColor.textBody).lineLimit(1)
                        Text("\(sec.segmentIndices?.count ?? 0) segments").font(VXFont.mono(10)).foregroundStyle(VXColor.textMuted)
                    }
                    .padding(.horizontal, 8).padding(.vertical, 7)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                    .onTapGesture { if let i = sec.segmentIndices?.first { state.selectedSegment = i } }
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 12)
        }
        .frame(width: VXMetrics.railSection)
        .background(VXColor.surfacePanel)
        .overlay(Rectangle().frame(width: 1).foregroundStyle(VXColor.borderDefault), alignment: .trailing)
    }
}

// ---------------------------------------------------------------- Job bar
struct JobBar: View {
    let job: JobInfo
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(job.stage ?? job.status).font(VXFont.sm).foregroundStyle(VXColor.textBody)
                Spacer()
                if let p = job.progress { Text("\(Int(p * 100))%").font(VXFont.mono(11)).foregroundStyle(VXColor.textMuted) }
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(VXColor.surfaceRaised)
                    Capsule().fill(VXColor.accent).frame(width: geo.size.width * (job.progress ?? 0.05))
                }
            }.frame(height: 5)
        }
    }
}
