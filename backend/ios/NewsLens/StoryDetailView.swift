import SwiftUI

/// The understanding journey: serif hero, corroboration ring, "for you" card,
/// expandable modules driving a sticky understanding pill, Ask-AI glass button.
struct StoryDetailView: View {
    let storyID: String
    @EnvironmentObject var api: APIClient
    @StateObject private var eng = Engagement.shared
    @State private var story: StoryDetail?
    @State private var error: String?
    @State private var opened: Set<String> = []
    @State private var moduleCount = 1
    @State private var toastMsg: String?
    @State private var showAsk = false
    @State private var celebrated = false

    private var progress: Double {
        min(1, 0.1 + 0.9 * Double(opened.count) / Double(max(moduleCount, 1)))
    }

    var body: some View {
        ZStack {
            InkBackground()
            if let s = story {
                loaded(s)
            } else if let error {
                ContentUnavailableView("Couldn't load story",
                                       systemImage: "exclamationmark.triangle",
                                       description: Text(error))
            } else {
                ProgressView().tint(BL.accent)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toast($toastMsg)
        .sensoryFeedback(.success, trigger: celebrated)
        .overlay(alignment: .bottomTrailing) { askButton }
        .sheet(isPresented: $showAsk) {
            AskAISheet(story: story).environmentObject(api)
        }
        .task {
            do {
                let s = try await api.fetchStory(id: storyID)
                story = s
                moduleCount = modules(for: s).count
                eng.explored(topic: s.topic)
            } catch { self.error = "Server unreachable." }
            await api.sendFeedback(storyID: storyID, action: "open")
        }
        .preferredColorScheme(.dark)
    }

    // MARK: - Layout

    private func loaded(_ s: StoryDetail) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                hero(s)
                if let impact = s.impactText, !impact.isEmpty { forYou(impact) }
                understandingPill
                ForEach(modules(for: s), id: \.id) { m in
                    ModuleCard(module: m, isOpen: opened.contains(m.id)) {
                        toggle(m.id)
                    }
                }
                Text("Narrative, personalization and analysis are AI-generated from the linked sources. The corroboration score measures source agreement, not absolute truth.")
                    .font(.caption2).foregroundStyle(BL.text2.opacity(0.7))
                    .padding(.top, 8)
            }
            .padding(.horizontal, 18)
            .padding(.bottom, 90)
        }
        .scrollIndicators(.hidden)
    }

    private func hero(_ s: StoryDetail) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Chip(text: s.topic.capitalized)
                Chip(text: s.credibility >= 75 ? "Balanced coverage" : "Developing story",
                     color: s.credibility >= 75 ? BL.trust : BL.warning, filled: true)
                Spacer()
                ImpactBadge(score: s.impactScore ?? 0)
            }
            Text(s.headline)
                .font(.system(.largeTitle, design: .serif, weight: .semibold))
                .lineSpacing(2)
            HStack(spacing: 14) {
                TrustRing(score: s.credibility)
                VStack(alignment: .leading, spacing: 2) {
                    Text("CORROBORATION SCORE")
                        .font(.caption2.weight(.bold)).foregroundStyle(BL.text2)
                        .kerning(1)
                    Text(s.credibilityNote ?? "Source agreement across ingested outlets")
                        .font(.caption).foregroundStyle(BL.text2)
                }
                Spacer()
            }
            .padding(14)
            .blCard(radius: 14)
        }
        .padding(.top, 8)
    }

    private func forYou(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("WHAT THIS MEANS FOR YOU", systemImage: "scope")
                .font(.caption2.weight(.bold)).kerning(1)
                .foregroundStyle(BL.accent)
            Text(text).font(.subheadline)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
            .fill(BL.aiGradient.opacity(0.13))
            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(BL.accent.opacity(0.3), lineWidth: 1)))
    }

    private var understandingPill: some View {
        HStack(spacing: 10) {
            Text("Understanding").font(.caption2).foregroundStyle(BL.text2)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.09))
                    Capsule().fill(BL.aiGradient)
                        .frame(width: geo.size.width * progress)
                }
            }
            .frame(height: 5)
            Text("\(Int(progress * 100))%")
                .font(.caption2.weight(.semibold).monospaced())
                .foregroundStyle(BL.accent)
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
        .blGlass(in: Capsule())
    }

    private var askButton: some View {
        Button {
            showAsk = true
        } label: {
            Label("Ask AI", systemImage: "sparkles")
                .font(.subheadline.weight(.semibold))
                .padding(.horizontal, 18).padding(.vertical, 13)
                .foregroundStyle(.white)
                .background(Capsule().fill(BL.aiGradient))
                .shadow(color: BL.ai.opacity(0.45), radius: 14, y: 6)
        }
        .padding(.trailing, 20).padding(.bottom, 24)
        .accessibilityHint("Ask the intelligence assistant about this story")
    }

    // MARK: - Modules

    private func toggle(_ id: String) {
        withAnimation(BL.spring) {
            if opened.contains(id) { opened.remove(id) } else {
                opened.insert(id)
                if opened.count >= moduleCount, !celebrated {
                    celebrated = true
                    eng.storyUnderstood()
                    toastMsg = "✓ Story understood — nicely done"
                }
            }
        }
    }

    private func modules(for s: StoryDetail) -> [StoryModule] {
        let paras = s.narrative.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
        let what = paras.first ?? s.narrative
        let why = paras.dropFirst().joined(separator: "\n\n")
        var mods: [StoryModule] = [
            .init(id: "what", title: "What happened", icon: "clock", tint: BL.accent,
                  content: .text(what)),
            .init(id: "why", title: "Why it matters", icon: "questionmark.circle", tint: BL.warning,
                  content: .text(why.isEmpty
                      ? "This story links to wider forces — open “The bigger picture” to see which trends it feeds and who is affected."
                      : why)),
        ]
        if let trends = s.trends, !trends.isEmpty {
            mods.append(.init(id: "big", title: "The bigger picture", icon: "chart.line.uptrend.xyaxis",
                              tint: BL.prediction, content: .trends(trends)))
        }
        if let conns = s.connections, !conns.isEmpty {
            mods.append(.init(id: "conn", title: "Hidden connections", icon: "point.3.connected.trianglepath.dotted",
                              tint: BL.ai, content: .connections(conns)))
        }
        mods.append(.init(id: "verify", title: "Claim check", icon: "checkmark.shield",
                          tint: BL.trust, content: .claims(s.claims)))
        if let sources = s.sources, !sources.isEmpty {
            mods.append(.init(id: "src", title: "Sources", icon: "link",
                              tint: BL.text2, content: .sources(sources)))
        }
        return mods
    }
}

// MARK: - Module model + card

struct StoryModule: Identifiable {
    enum Content {
        case text(String)
        case trends([StoryDetail.StoryTrend])
        case connections([StoryDetail.Connection])
        case claims(StoryDetail.Claims?)
        case sources([StoryDetail.Source])
    }
    let id: String
    let title: String
    let icon: String
    let tint: Color
    let content: Content
}

struct ModuleCard: View {
    let module: StoryModule
    let isOpen: Bool
    let onTap: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: onTap) {
                HStack(spacing: 12) {
                    Image(systemName: module.icon)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(module.tint)
                        .frame(width: 34, height: 34)
                        .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(module.tint.opacity(0.13)))
                    Text(module.title).font(.subheadline.weight(.semibold))
                    Spacer()
                    Image(systemName: "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(BL.text2)
                        .rotationEffect(.degrees(isOpen ? 180 : 0))
                }
                .padding(16)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(isOpen ? [.isSelected] : [])
            .sensoryFeedback(.impact(weight: .light), trigger: isOpen)

            if isOpen {
                body(for: module.content)
                    .padding(.horizontal, 16).padding(.bottom, 16)
                    .transition(.asymmetric(insertion: .opacity.combined(with: .move(edge: .top)),
                                            removal: .opacity))
            }
        }
        .blCard()
    }

    @ViewBuilder
    private func body(for content: StoryModule.Content) -> some View {
        switch content {
        case .text(let t):
            Text(t).font(.subheadline).foregroundStyle(BL.text2).lineSpacing(3)

        case .trends(let trends):
            VStack(spacing: 10) {
                ForEach(trends, id: \.id) { t in
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(t.name).font(.footnote.weight(.semibold))
                                if t.kind == "micro" {
                                    Chip(text: "EARLY SIGNAL", color: BL.prediction, filled: true)
                                }
                            }
                            Text(t.narrative).font(.caption).foregroundStyle(BL.text2)
                        }
                        Spacer()
                        Sparkline(seed: t.name, color: BL.prediction)
                    }
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(BL.surface2))
                }
            }

        case .connections(let conns):
            VStack(alignment: .leading, spacing: 10) {
                Text("AI-inferred hypotheses — links that aren't obvious but may matter. Treat as leads, not facts.")
                    .font(.caption).foregroundStyle(BL.text2)
                ForEach(conns, id: \.self) { c in
                    VStack(alignment: .leading, spacing: 5) {
                        Label(c.otherTitle, systemImage: "arrow.left.arrow.right")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(BL.prediction)
                        Text(c.chain).font(.caption).foregroundStyle(BL.text2)
                        Text("confidence \(Int(c.confidence * 100))%")
                            .font(.caption2.monospaced()).foregroundStyle(BL.prediction)
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(BL.ai.opacity(0.08))
                        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .stroke(BL.ai.opacity(0.22), lineWidth: 1)))
                }
            }

        case .claims(let claims):
            let verdicts = claims?.verdicts ?? (claims?.claims ?? []).map {
                StoryDetail.Verdict(claim: $0, verdict: "unverified", note: "Not yet assessed")
            }
            VStack(alignment: .leading, spacing: 10) {
                ForEach(verdicts, id: \.self) { v in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: icon(for: v.verdict))
                            .foregroundStyle(color(for: v.verdict))
                            .font(.footnote)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(v.claim).font(.footnote)
                            Text("\(v.verdict) — \(v.note)")
                                .font(.caption2).foregroundStyle(BL.text2)
                        }
                    }
                }
            }

        case .sources(let sources):
            VStack(spacing: 0) {
                ForEach(sources, id: \.self) { src in
                    if let urlStr = src.url, let url = URL(string: urlStr) {
                        Link(destination: url) {
                            HStack(spacing: 10) {
                                Circle().fill(BL.accent).frame(width: 7, height: 7)
                                Text(src.title ?? urlStr)
                                    .font(.footnote).foregroundStyle(.white)
                                    .multilineTextAlignment(.leading)
                                Spacer()
                                Text(src.source ?? "")
                                    .font(.caption2.monospaced()).foregroundStyle(BL.text2)
                            }
                            .padding(.vertical, 9)
                        }
                    }
                }
            }
        }
    }

    private func icon(for verdict: String) -> String {
        switch verdict {
        case "corroborated": return "checkmark.circle.fill"
        case "disputed": return "xmark.circle.fill"
        default: return "questionmark.circle.fill"
        }
    }
    private func color(for verdict: String) -> Color {
        switch verdict {
        case "corroborated": return BL.trust
        case "disputed": return BL.breaking
        default: return BL.warning
        }
    }
}

// MARK: - Ask AI sheet

struct AskAISheet: View {
    var story: StoryDetail?
    @EnvironmentObject var api: APIClient
    @Environment(\.dismiss) private var dismiss

    struct Message: Identifiable {
        let id = UUID()
        let isUser: Bool
        var text: String
    }

    @State private var messages: [Message] = []
    @State private var suggestions = ["Explain like I'm 15", "What happens next?",
                                      "Why does this matter to me?"]
    @State private var input = ""
    @State private var thinking = false

    var body: some View {
        NavigationStack {
            ZStack {
                InkBackground()
                VStack(spacing: 0) {
                    ScrollViewReader { proxy in
                        ScrollView {
                            VStack(spacing: 10) {
                                ForEach(messages) { m in bubble(m) }
                                if thinking {
                                    HStack { ProgressView().tint(BL.ai); Spacer() }
                                        .padding(.horizontal, 4)
                                }
                            }
                            .padding(16)
                            .id("bottom")
                        }
                        .onChange(of: messages.count) {
                            withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                        }
                    }
                    if !suggestions.isEmpty {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(suggestions, id: \.self) { s in
                                    Button { send(s) } label: {
                                        Chip(text: s, color: BL.ai, filled: true)
                                    }
                                }
                            }
                            .padding(.horizontal, 16)
                        }
                        .padding(.bottom, 8)
                    }
                    HStack(spacing: 8) {
                        TextField("Ask about this story…", text: $input)
                            .textFieldStyle(.plain)
                            .padding(.horizontal, 14).padding(.vertical, 11)
                            .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .fill(BL.surface2))
                            .onSubmit { send(input) }
                        Button { send(input) } label: {
                            Image(systemName: "arrow.up")
                                .font(.subheadline.weight(.bold))
                                .foregroundStyle(.white)
                                .frame(width: 40, height: 40)
                                .background(Circle().fill(BL.aiGradient))
                        }
                        .disabled(input.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    .padding(12)
                }
            }
            .navigationTitle("Intelligence Assistant")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .onAppear {
                if messages.isEmpty {
                    let intro = story.map { "I've read “\($0.headline)”. What would you like to understand?" }
                        ?? "Ask me anything about today's news."
                    messages.append(.init(isUser: false, text: intro))
                }
            }
        }
        .preferredColorScheme(.dark)
    }

    private func bubble(_ m: Message) -> some View {
        HStack {
            if m.isUser { Spacer(minLength: 40) }
            Text(m.text)
                .font(.subheadline)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(m.isUser ? BL.accent.opacity(0.2) : BL.surface2))
                .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .stroke(m.isUser ? BL.accent.opacity(0.35) : BL.hairline, lineWidth: 1))
            if !m.isUser { Spacer(minLength: 40) }
        }
    }

    private func send(_ text: String) {
        let q = text.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty, !thinking else { return }
        input = ""
        suggestions = []
        messages.append(.init(isUser: true, text: q))
        thinking = true
        Task {
            do {
                let r = try await api.ask(q, storyID: story?.id)
                messages.append(.init(isUser: false, text: r.answer))
                suggestions = r.followups ?? []
            } catch {
                messages.append(.init(isUser: false,
                    text: "I can't reach the backend right now — start it and try again."))
            }
            thinking = false
        }
    }
}
