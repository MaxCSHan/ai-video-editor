import Foundation

/// Codable mirrors of the pipeline's `EditorialStoryboard` (models.py). Field
/// names match the JSON served by `GET /projects/{id}/storyboard` — verified
/// against the real `myanmar` project. All timestamps are seconds.
struct Storyboard: Codable, Hashable {
    var title: String
    var editorialReasoning: String
    var estimatedDurationSec: Double
    var style: String
    var storyConcept: String
    var cast: [CastMember]
    var storyArc: [StoryArcSection]
    var segments: [Segment]
    var discarded: [DiscardedClip]
    var musicPlan: [MusicCue]
    var technicalNotes: [String]
    var pacingNotes: [String]

    enum CodingKeys: String, CodingKey {
        case title
        case editorialReasoning = "editorial_reasoning"
        case estimatedDurationSec = "estimated_duration_sec"
        case style
        case storyConcept = "story_concept"
        case cast
        case storyArc = "story_arc"
        case segments
        case discarded
        case musicPlan = "music_plan"
        case technicalNotes = "technical_notes"
        case pacingNotes = "pacing_notes"
    }

    var totalDuration: Double { segments.reduce(0) { $0 + $1.duration } }
}

struct Segment: Codable, Hashable, Identifiable {
    var index: Int
    var clipID: String
    var inSec: Double
    var outSec: Double
    var purpose: String
    var description: String
    var transition: String
    var audioNote: String
    var textOverlay: String

    var id: Int { index }
    var duration: Double { max(0, outSec - inSec) }
    var purposeKind: Purpose { Purpose.from(purpose) }

    enum CodingKeys: String, CodingKey {
        case index
        case clipID = "clip_id"
        case inSec = "in_sec"
        case outSec = "out_sec"
        case purpose, description, transition
        case audioNote = "audio_note"
        case textOverlay = "text_overlay"
    }
}

struct CastMember: Codable, Hashable {
    var name: String
    var description: String?
    var role: String?
    var appearsIn: [String]?
    enum CodingKeys: String, CodingKey {
        case name, description, role
        case appearsIn = "appears_in"
    }
}

struct StoryArcSection: Codable, Hashable {
    var title: String?
    var description: String?
    var segmentIndices: [Int]?
    enum CodingKeys: String, CodingKey {
        case title, description
        case segmentIndices = "segment_indices"
    }
}

struct DiscardedClip: Codable, Hashable {
    var clipID: String?
    var reason: String?
    enum CodingKeys: String, CodingKey {
        case clipID = "clip_id"
        case reason
    }
}

struct MusicCue: Codable, Hashable {
    var section: String?
    var strategy: String?
    var notes: String?
}
