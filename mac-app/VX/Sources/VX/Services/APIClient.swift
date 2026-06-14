import Foundation

/// Thin async REST client for the VX sidecar (loopback). Mutating calls return
/// a `JobInfo`; progress is streamed separately via `JobStream`.
actor APIClient {
    static let shared = APIClient()

    var baseURL = URL(string: "http://127.0.0.1:8765")!
    private let session = URLSession(configuration: .default)

    func setBaseURL(_ url: URL) { baseURL = url }

    struct APIError: LocalizedError {
        let status: Int
        let body: String
        var errorDescription: String? { "HTTP \(status): \(body)" }
    }

    private func decode<T: Decodable>(_ data: Data, _ resp: URLResponse) throws -> T {
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let code = (resp as? HTTPURLResponse)?.statusCode ?? -1
            throw APIError(status: code, body: String(data: data, encoding: .utf8) ?? "")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        let (data, resp) = try await session.data(from: baseURL.appendingPathComponent(path))
        return try decode(data, resp)
    }

    private func post<T: Decodable, B: Encodable>(_ path: String, _ body: B) async throws -> T {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(body)
        let (data, resp) = try await session.data(for: req)
        return try decode(data, resp)
    }

    // -- Reads ---------------------------------------------------------------
    struct Health: Codable { let ok: Bool; let library: String }
    func health() async throws -> Health { try await get("/health") }

    func projects() async throws -> [ProjectSummary] { try await get("/projects") }
    func project(_ id: String) async throws -> ProjectDetail { try await get("/projects/\(id)") }
    func storyboard(_ id: String) async throws -> Storyboard { try await get("/projects/\(id)/storyboard") }
    func cost(_ id: String) async throws -> CostSummary { try await get("/projects/\(id)/cost") }

    // -- Mutations (return jobs) --------------------------------------------
    func createProject(_ req: CreateProjectRequest) async throws -> JobInfo { try await post("/projects", req) }
    func analyze(_ id: String, _ req: AnalyzeRequest) async throws -> JobInfo { try await post("/projects/\(id)/analyze", req) }
    func cut(_ id: String, _ req: CutRequest) async throws -> JobInfo { try await post("/projects/\(id)/cut", req) }
    func job(_ id: String) async throws -> JobInfo { try await get("/jobs/\(id)") }

    func proxyURL(project: String, clip: String) -> URL {
        baseURL.appendingPathComponent("media/proxy/\(project)/\(clip)")
    }
    func roughCutURL(project: String) -> URL {
        baseURL.appendingPathComponent("media/roughcut/\(project)")
    }
    func jobWebSocketURL(_ id: String) -> URL {
        var c = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        c.scheme = baseURL.scheme == "https" ? "wss" : "ws"
        c.path = "/jobs/\(id)/ws"
        return c.url!
    }
}

// Request bodies (mirror server/schemas.py)
struct CreateProjectRequest: Codable {
    var name: String
    var sourceDir: String
    var provider: String = "gemini"
    var style: String = "vlog"
    var includedClips: [String]? = nil
    enum CodingKeys: String, CodingKey {
        case name
        case sourceDir = "source_dir"
        case provider, style
        case includedClips = "included_clips"
    }
}

struct AnalyzeRequest: Codable {
    var provider: String? = nil
    var force: Bool = false
    var visual: Bool = false
    var timeline: Bool = false
    var maxCost: Double? = nil
    enum CodingKeys: String, CodingKey {
        case provider, force, visual, timeline
        case maxCost = "max_cost"
    }
}

struct CutRequest: Codable {
    var proxyMode: Bool = false
    var storyboardVersion: Int? = nil
    enum CodingKeys: String, CodingKey {
        case proxyMode = "proxy_mode"
        case storyboardVersion = "storyboard_version"
    }
}
