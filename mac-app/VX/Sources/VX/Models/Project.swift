import Foundation

/// DTOs mirroring `server/schemas.py`.
struct ProjectSummary: Codable, Identifiable, Hashable {
    var id: String
    var name: String
    var type: String
    var provider: String
    var style: String?
    var mode: String
    var clipCount: Int
    var createdAt: String?
    var stylePreset: String?
    var hasStoryboard: Bool
    var hasRoughCut: Bool
    var latestVersion: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, type, provider, style, mode
        case clipCount = "clip_count"
        case createdAt = "created_at"
        case stylePreset = "style_preset"
        case hasStoryboard = "has_storyboard"
        case hasRoughCut = "has_rough_cut"
        case latestVersion = "latest_version"
    }

    var isTimeline: Bool { mode == "timeline" }
}

struct ProjectDetail: Codable, Hashable {
    var id: String
    var name: String
    var type: String
    var provider: String
    var mode: String
    var clipCount: Int
    var hasStoryboard: Bool
    var hasRoughCut: Bool
    var latestVersion: Int?
    var clips: [String]
    var storyboardPath: String?
    var roughCutPath: String?

    enum CodingKeys: String, CodingKey {
        case id, name, type, provider, mode, clips
        case clipCount = "clip_count"
        case hasStoryboard = "has_storyboard"
        case hasRoughCut = "has_rough_cut"
        case latestVersion = "latest_version"
        case storyboardPath = "storyboard_path"
        case roughCutPath = "rough_cut_path"
    }
}

struct CostSummary: Codable, Hashable {
    var calls: Int
    var totalTokens: Int
    var inputTokens: Int
    var outputTokens: Int
    var estimatedCostUSD: Double
    var byPhase: [String: PhaseCost]?

    enum CodingKeys: String, CodingKey {
        case calls
        case totalTokens = "total_tokens"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case estimatedCostUSD = "estimated_cost_usd"
        case byPhase = "by_phase"
    }

    static let zero = CostSummary(calls: 0, totalTokens: 0, inputTokens: 0,
                                  outputTokens: 0, estimatedCostUSD: 0, byPhase: nil)
}

struct PhaseCost: Codable, Hashable {
    var calls: Int
    var totalTokens: Int
    var estimatedCostUSD: Double
    enum CodingKeys: String, CodingKey {
        case calls
        case totalTokens = "total_tokens"
        case estimatedCostUSD = "estimated_cost_usd"
    }
}

struct JobInfo: Codable, Hashable, Identifiable {
    var id: String
    var kind: String
    var project: String
    var status: String
    var stage: String?
    var progress: Double?
    var error: String?
    var result: [String: AnyCodable]?
    var cost: CostSummary?
    var logTail: [String]?
    var durationSec: Double?

    enum CodingKeys: String, CodingKey {
        case id, kind, project, status, stage, progress, error, result, cost
        case logTail = "log_tail"
        case durationSec = "duration_sec"
    }

    var isTerminal: Bool { status == "completed" || status == "failed" }
}

/// Minimal type-erased JSON value so `result` (an open dict) decodes cleanly.
struct AnyCodable: Codable, Hashable {
    let value: String
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let s = try? c.decode(String.self) { value = s }
        else if let i = try? c.decode(Int.self) { value = String(i) }
        else if let d = try? c.decode(Double.self) { value = String(d) }
        else if let b = try? c.decode(Bool.self) { value = String(b) }
        else { value = "" }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(value)
    }
}
