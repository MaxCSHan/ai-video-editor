import SwiftUI

enum Route: Hashable { case library, briefing, editor, settings }

/// Central observable store. Owns navigation, the loaded project + storyboard,
/// live cost, and the currently running job.
@MainActor
final class AppState: ObservableObject {
    @Published var route: Route = .library
    @Published var projects: [ProjectSummary] = []
    @Published var activeProject: ProjectSummary?
    @Published var detail: ProjectDetail?
    @Published var storyboard: Storyboard?
    @Published var cost: CostSummary = .zero
    @Published var selectedSegment: Int?
    @Published var editMode: String = "story"   // "story" | "timeline"

    @Published var connected = false
    @Published var loadError: String?
    @Published var currentJob: JobInfo?

    private let api = APIClient.shared

    // -- Lifecycle -----------------------------------------------------------
    func bootstrap() async {
        await waitForSidecar()
        await reloadProjects()
    }

    func waitForSidecar() async {
        for _ in 0..<40 {
            if (try? await api.health()) != nil { connected = true; return }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        connected = false
        loadError = "Could not reach the VX sidecar on 127.0.0.1:8765. Run `vx serve` (or `python -m ai_video_editor.server`)."
    }

    func reloadProjects() async {
        do { projects = try await api.projects(); loadError = nil }
        catch { loadError = "\(error.localizedDescription)" }
    }

    // -- Navigation ----------------------------------------------------------
    func open(_ p: ProjectSummary) {
        activeProject = p
        editMode = p.mode
        route = .editor
        Task { await loadProject(p.id) }
    }

    func loadProject(_ id: String) async {
        async let d = try? await api.project(id)
        async let sb = try? await api.storyboard(id)
        async let c = try? await api.cost(id)
        detail = await d
        storyboard = await sb
        cost = await c ?? .zero
        selectedSegment = storyboard?.segments.first?.index
    }

    func refreshCost() async {
        guard let id = activeProject?.id else { return }
        if let c = try? await api.cost(id) { cost = c }
    }

    // -- Jobs ----------------------------------------------------------------
    func runAnalyze(timeline: Bool, visual: Bool) {
        guard let id = activeProject?.id else { return }
        Task { await track(try await api.analyze(id, AnalyzeRequest(visual: visual, timeline: timeline))) }
    }

    func runCut(proxyMode: Bool) {
        guard let id = activeProject?.id else { return }
        Task { await track(try await api.cut(id, CutRequest(proxyMode: proxyMode))) }
    }

    private func track(_ job: JobInfo) async {
        currentJob = job
        let stream = await JobStream(jobID: job.id)
        for await update in stream.updates() {
            currentJob = update
            if let c = update.cost { cost = c }
            if update.isTerminal {
                if let id = activeProject?.id { await loadProject(id) }
                await reloadProjects()
            }
        }
    }
}
