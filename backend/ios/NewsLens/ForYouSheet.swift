import SwiftUI

/// "Read for you" — the personal impact page (mockup 1h, third panel).
///
/// Drawn dark on every skin and in both appearance modes. That is not a bug and
/// not a dark-mode variant: the design inverts this one page on purpose,
/// because it is the only page that is about the reader rather than the event,
/// and the change of ground is what says so before a word is read. Its colours
/// are therefore fixed here rather than read from `Palette` — the palette
/// describes the paper, and this page is deliberately not on the paper.
struct ForYouSheet: View {
    let story: StoryDetail?
    var loading: Bool
    var checked: Bool

    @EnvironmentObject var api: APIClient
    @Environment(\.dismiss) private var dismiss
    @AppStorage("onboarded") private var onboarded = false
    @State private var showEdit = false

    // The night-desk ground, from the mockup.
    private let ground = Color.hex(0x17150F)
    private let paper = Color.hex(0xF7F4EE)
    private let gold = Color.hex(0xC8B98F)
    private var dim: Color { paper.opacity(0.78) }
    private var faint: Color { paper.opacity(0.55) }
    private var rule: Color { paper.opacity(0.14) }

    private var lens: UserContext? {
        guard let d = UserDefaults.standard.data(forKey: "saved_context") else { return nil }
        return try? JSONDecoder().decode(UserContext.self, from: d)
    }

    var body: some View {
        ZStack {
            ground.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    head
                    content
                    certainty
                    gaps
                }
                .padding(.horizontal, 22)
                .padding(.bottom, 34)
            }
            .scrollIndicators(.hidden)
        }
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .sheet(isPresented: $showEdit) {
            OnboardingView(initial: lens) {
                onboarded = true
                showEdit = false
            }
            .environmentObject(api).skinned()
        }
    }

    private var head: some View {
        HStack {
            Text("Read for you")
                .font(.system(size: 18, design: .serif))
                .foregroundStyle(paper)
            Spacer()
            Button("edit") { showEdit = true }
                .font(.system(size: 13))
                .foregroundStyle(faint)
                .accessibilityLabel("Edit what Descry knows about you")
        }
        .padding(.top, 16)
        .padding(.bottom, 22)
    }

    @ViewBuilder
    private var content: some View {
        if let text = story?.impactText, !text.isEmpty {
            Text(text)
                .font(.system(size: 17, weight: .light, design: .serif))
                .lineSpacing(9)
                .foregroundStyle(dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 24)
        } else if loading {
            HStack(spacing: 10) {
                ProgressView().tint(gold)
                Text("Reading this one against your world…")
                    .font(.system(size: 15))
                    .foregroundStyle(dim)
            }
            .padding(.bottom, 24)
        } else if !api.isGoogleUser || !onboarded {
            Text("Descry doesn't know your world yet. Tell it once — your work, your city, what you follow — and every story explains what it means for you.")
                .font(.system(size: 17, weight: .light, design: .serif))
                .lineSpacing(9)
                .foregroundStyle(dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 20)
            Button { showEdit = true } label: {
                Text("Set up your lens")
                    .font(.system(size: 14.5, weight: .medium))
                    .foregroundStyle(ground)
                    .padding(.horizontal, 18).padding(.vertical, 11)
                    .background(Capsule().fill(paper))
            }
            .buttonStyle(.plain)
            .padding(.bottom, 24)
        } else if checked {
            // A real answer, not a failure: the personalizer looked and found
            // nothing that touches this reader. Saying so is more use than an
            // invented connection would be.
            Text("This one doesn't touch your work, your city or anything you follow. Descry says so rather than inventing a reason it should matter to you.")
                .font(.system(size: 17, weight: .light, design: .serif))
                .lineSpacing(9)
                .foregroundStyle(dim)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 24)
        }
    }

    // MARK: - How sure are we?

    /// The mockup prints a five-segment certainty and a sentence naming what we
    /// know and what we are guessing at. That is computable and honest: how far
    /// a personal read can be trusted is exactly a function of how much of your
    /// lens you have actually filled in, and this counts the fields rather than
    /// scoring the prose. Nothing here is a claim about the model's accuracy —
    /// we have never measured that, and a bar implying we had would be a lie.
    @ViewBuilder
    private var certainty: some View {
        let known = knownFields
        if !known.have.isEmpty || !known.missing.isEmpty,
           story?.impactText?.isEmpty == false {
            VStack(alignment: .leading, spacing: 10) {
                Text("How sure are we?")
                    .font(.system(size: 16, design: .serif))
                    .foregroundStyle(paper)
                HStack(spacing: 3) {
                    ForEach(0..<5, id: \.self) { i in
                        RoundedRectangle(cornerRadius: 2)
                            .fill(i < known.lit ? gold : paper.opacity(0.18))
                            .frame(height: 5)
                    }
                }
                Text(sentence(known))
                    .font(.system(size: 13.5))
                    .lineSpacing(5)
                    .foregroundStyle(paper.opacity(0.62))
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8).fill(paper.opacity(0.06)))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(rule, lineWidth: 1))
        }
    }

    /// Which parts of the lens are filled in. `lit` is a count of what we have,
    /// out of five — not a confidence the model reported.
    private var knownFields: (have: [String], missing: [String], lit: Int) {
        guard let c = lens else { return ([], [], 1) }
        var have: [String] = [], missing: [String] = []
        (c.profession.isEmpty ? { missing.append("your work") } : { have.append("your work") })()
        (c.location.city.isEmpty ? { missing.append("your city") } : { have.append("your city") })()
        (c.interests.isEmpty ? { missing.append("what you follow") } : { have.append("what you follow") })()
        (c.roleSeniority.isEmpty ? { missing.append("how senior you are") } : { have.append("how senior you are") })()
        (c.lineOfBusiness.isEmpty ? { missing.append("your industry") } : { have.append("your industry") })()
        return (have, missing, max(1, have.count))
    }

    private func sentence(_ k: (have: [String], missing: [String], lit: Int)) -> String {
        let confidence = k.lit >= 4 ? "Fairly sure" : k.lit >= 2 ? "Only roughly" : "Barely at all"
        var s = "\(confidence) — we know \(list(k.have))"
        if !k.missing.isEmpty { s += ", not \(list(k.missing))" }
        s += ". Treat this as something to think about, not an answer."
        return s
    }

    private func list(_ items: [String]) -> String {
        switch items.count {
        case 0: return "almost nothing about you"
        case 1: return items[0]
        default: return items.dropLast().joined(separator: ", ") + " and " + items.last!
        }
    }

    // MARK: - What would sharpen it

    /// The mockup's chips ("Tell us what tools you use", "Read as a parent")
    /// are offers to fill a gap. They are generated from the gaps this reader
    /// actually has, so a fully-filled lens is offered nothing instead of being
    /// asked for something it already gave.
    @ViewBuilder
    private var gaps: some View {
        let missing = knownFields.missing
        if !missing.isEmpty, api.isGoogleUser {
            VStack(alignment: .leading, spacing: 10) {
                Text("Sharpen this")
                    .font(.system(size: 12, weight: .medium, design: .monospaced))
                    .kerning(1.68)
                    .textCase(.uppercase)
                    .foregroundStyle(faint)
                BLFlow(spacing: 7, lineSpacing: 7) {
                    ForEach(missing, id: \.self) { gap in
                        Button { showEdit = true } label: {
                            Text("Tell us \(gap)")
                                .font(.system(size: 13.5))
                                .foregroundStyle(paper.opacity(0.85))
                                .padding(.horizontal, 13).padding(.vertical, 7)
                                .overlay(Capsule().stroke(paper.opacity(0.28), lineWidth: 1))
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(.top, 20)
        }
    }
}
