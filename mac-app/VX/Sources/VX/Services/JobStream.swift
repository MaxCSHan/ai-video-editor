import Foundation

/// Streams `JobInfo` updates from the sidecar's `/jobs/{id}/ws` WebSocket as an
/// async sequence. Closes when the job reaches a terminal state.
final class JobStream {
    private let task: URLSessionWebSocketTask
    private let decoder = JSONDecoder()

    init(jobID: String) async {
        let url = await APIClient.shared.jobWebSocketURL(jobID)
        task = URLSession.shared.webSocketTask(with: url)
        task.resume()
    }

    /// Yields each `JobInfo` snapshot until the socket closes or the job ends.
    func updates() -> AsyncStream<JobInfo> {
        AsyncStream { continuation in
            Task {
                while true {
                    do {
                        let message = try await task.receive()
                        switch message {
                        case .string(let text):
                            if let data = text.data(using: .utf8),
                               let info = try? decoder.decode(JobInfo.self, from: data) {
                                continuation.yield(info)
                                if info.isTerminal { continuation.finish(); return }
                            }
                        case .data(let data):
                            if let info = try? decoder.decode(JobInfo.self, from: data) {
                                continuation.yield(info)
                                if info.isTerminal { continuation.finish(); return }
                            }
                        @unknown default:
                            break
                        }
                    } catch {
                        continuation.finish()
                        return
                    }
                }
            }
            continuation.onTermination = { [task] _ in
                task.cancel(with: .goingAway, reason: nil)
            }
        }
    }

    func cancel() { task.cancel(with: .goingAway, reason: nil) }
}
