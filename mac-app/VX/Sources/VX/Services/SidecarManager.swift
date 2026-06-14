import Foundation

/// Optionally launches the Python sidecar as a child process if one isn't
/// already reachable. In dev you typically run `vx serve` yourself; for a
/// shipping `.app` this would spawn a bundled interpreter. Controlled by the
/// `VX_AUTOSPAWN_SIDECAR` env var (default: off, since dev runs it manually).
final class SidecarManager {
    static let shared = SidecarManager()
    private var process: Process?

    /// Repo root is inferred from VX_REPO env or the current working directory.
    var repoRoot: URL {
        if let p = ProcessInfo.processInfo.environment["VX_REPO"] { return URL(fileURLWithPath: p) }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    }

    func startIfNeeded() {
        guard ProcessInfo.processInfo.environment["VX_AUTOSPAWN_SIDECAR"] == "1" else { return }
        let proc = Process()
        proc.currentDirectoryURL = repoRoot
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        proc.arguments = ["python", "-m", "ai_video_editor.server"]
        var env = ProcessInfo.processInfo.environment
        env["VX_PORT"] = env["VX_PORT"] ?? "8765"
        proc.environment = env
        do { try proc.run(); process = proc }
        catch { NSLog("VX: failed to spawn sidecar: \(error)") }
    }

    func stop() {
        process?.terminate()
        process = nil
    }
}
