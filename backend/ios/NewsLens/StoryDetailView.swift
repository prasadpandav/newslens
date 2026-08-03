import SwiftUI

/// The understanding journey: serif hero, corroboration ring, "for you" card,
/// expandable modules driving a sticky understanding pill, Ask-AI glass button.
struct StoryDetailView: View {
    @Environment(\.palette) private var pal

    let storyID: String
    @EnvironmentObject var api: APIClient
    @StateObject private var eng = Engagement.shared
    @AppStorage("onboarded") private var onboarded = false
    @State private var story: StoryDetail?
    @State private var error: String?
    /// "What this means for you" hasn't been fetched (nil), is in flight
    /// (true), or came back — possibly empty, which is a real answer ("this
    /// story doesn't touch your interests"), not "not asked yet". Kept
    /// separate from `story.impactText` so an empty string from the server
    /// can be told apart from never having asked.
    @State private var forYouLoading = false
    @State private var forYouChecked = false
    /// Which modules are expanded right now — pure UI state, goes up and down.
    @State private var opened: Set<String> = []
    /// Which modules the reader has ever opened. Progress reads from this, never
    /// from `opened`: collapsing a card you've already read is tidying up, not
    /// un-reading it, so the meter must not fall back.
    @State private var credited: Set<String> = []
    @State private var moduleCount = 1
    @State private var toastMsg: String?
    @State private var showAsk = false
    @State private var celebrated = false

    private var progress: Double {
        min(1, 0.1 + 0.9 * Double(credited.count) / Double(max(moduleCount, 1)))
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
                ProgressView().tint(pal.accent)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                if let s = story {
                    ShareLink(item: api.shareURL("story/\(s.id)"),
                              subject: Text(s.headline),
                              message: Text("\(s.headline) — via Descry")) {
                        Image(systemName: "square.and.arrow.up")
                            .foregroundStyle(pal.accent)
                    }
                    .accessibilityLabel("Share this story")
                }
                Button {
                    Task {
                        await api.toggleBookmark(storyID: storyID)
                        toastMsg = api.savedStoryIDs.contains(storyID)
                            ? "Saved for later" : "Removed from saved"
                    }
                } label: {
                    Image(systemName: api.savedStoryIDs.contains(storyID)
                          ? "bookmark.fill" : "bookmark")
                        .foregroundStyle(pal.accent)
                }
                .accessibilityLabel(api.savedStoryIDs.contains(storyID)
                                    ? "Remove from saved" : "Save for later")
                .sensoryFeedback(.impact(weight: .light),
                                 trigger: api.savedStoryIDs.contains(storyID))
            }
        }
        .toast($toastMsg)
        .sensoryFeedback(.success, trigger: celebrated)
        .overlay(alignment: .bottomTrailing) { askButton }
        .sheet(isPresented: $showAsk) {
            AskAISheet(story: story).environmentObject(api).skinned()
        }
        .task {
            do {
                let s = try await api.fetchStory(id: storyID)
                story = s
                moduleCount = modules(for: s).count
                eng.explored(topic: s.topic)
                // Open "What happened" by default so the reader gets the story
                // in one go; the rest stays collapsed to invite exploration.
                withAnimation(BL.spring.delay(0.3)) { _ = opened.insert("what") }
                credit("what")
            } catch { self.error = "Server unreachable." }
            await api.sendFeedback(storyID: storyID, action: "open")
        }
    }

    // MARK: - Layout

    private func loaded(_ s: StoryDetail) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                hero(s)
                ForEach(modules(for: s), id: \.id) { m in
                    ModuleCard(module: m, isOpen: opened.contains(m.id)) {
                        toggle(m.id)
                    }
                }
                Text("Narrative, personalization and analysis are AI-generated from the linked sources. The corroboration score measures source agreement, not absolute truth.")
                    .font(.caption2).foregroundStyle(pal.text2.opacity(0.7))
                    .padding(.top, 8)
            }
            .padding(.horizontal, 18)
            .padding(.bottom, 90)
        }
        .scrollIndicators(.hidden)
        // Pinned above the content: always-visible reading progress.
        .safeAreaInset(edge: .top, spacing: 0) {
            understandingPill
                .padding(.horizontal, 40)
                .padding(.vertical, 4)
        }
    }

    private func hero(_ s: StoryDetail) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            // Wrapping layout: with the timestamp added these no longer fit on
            // one line at larger text sizes.
            BLFlow(spacing: 8) {
                Chip(text: s.topic.topicLabel)
                Chip(text: s.credibility >= 75 ? "Balanced coverage" : "Developing story",
                     color: s.credibility >= 75 ? pal.trust : pal.warning, filled: true)
                LastToldChip(at: s.createdAt)
                ImpactBadge(score: s.impactScore ?? 0)
            }
            Text(s.headline)
                .font(.system(.title, design: .serif, weight: .semibold))
                .lineSpacing(2)
                .lineLimit(4)
                .minimumScaleFactor(0.8)
            // Taller than the feed card's thumbnail — this is the reader.
            // Self-collapsing, so a story with no artwork loses nothing.
            StoryImage(urlString: s.imageUrl, height: 220)
            HStack(spacing: 14) {
                TrustRing(score: s.credibility)
                VStack(alignment: .leading, spacing: 2) {
                    Text("CORROBORATION SCORE")
                        .font(.caption2.weight(.bold)).foregroundStyle(pal.text2)
                        .kerning(1)
                    Text(s.credibilityNote ?? "Source agreement across ingested outlets")
                        .font(.caption).foregroundStyle(pal.text2)
                }
                Spacer()
            }
            .padding(14)
            .blCard(radius: 14)
        }
        .padding(.top, 8)
    }

    private var understandingPill: some View {
        HStack(spacing: 10) {
            Text("Story completed").font(.caption2).foregroundStyle(pal.text2)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    // The last of the hardcoded white tracks: invisible against a
                    // white card in light mode and against paper in both skins.
                    Capsule().fill(pal.surface2)
                    Capsule().fill(pal.aiGradient)
                        .frame(width: geo.size.width * progress)
                }
            }
            .frame(height: 5)
            Text("\(Int(progress * 100))%")
                .font(.caption2.weight(.semibold).monospaced())
                .foregroundStyle(pal.accent)
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
                .background(Capsule().fill(pal.aiGradient))
                .shadow(color: pal.glow(pal.ai, 0.45), radius: 14, y: 6)
        }
        .padding(.trailing, 20).padding(.bottom, 24)
        .accessibilityHint("Ask the intelligence assistant about this story")
    }

    // MARK: - Modules

    private func toggle(_ id: String) {
        let opening = !opened.contains(id)
        withAnimation(BL.spring) {
            if opened.contains(id) { opened.remove(id) } else {
                opened.insert(id)
                credit(id)
            }
        }
        if opening && id == "foryou" { Task { await loadPersonalize() } }
    }

    /// Fires once per view, the first time the reader opens "What this means
    /// for you" and nothing was already cached from a previous open — see
    /// APIClient.personalizeStory. Guarded so re-toggling the module closed
    /// and back open doesn't refetch.
    private func loadPersonalize() async {
        guard let s = story, (s.impactText ?? "").isEmpty,
              !forYouLoading, !forYouChecked else { return }
        forYouLoading = true
        let result = await api.personalizeStory(storyID: s.id)
        forYouLoading = false
        guard let result else { return }   // network hiccup — leave it retryable
        story?.impactText = result.text
        story?.impactScore = result.score
        forYouChecked = true
    }

    /// Count a module toward the meter. Separate from `toggle` so a module that
    /// starts expanded ("What happened") can count without being flipped shut,
    /// and so collapsing never takes the credit back.
    private func credit(_ id: String) {
        guard !credited.contains(id) else { return }
        credited.insert(id)
        if credited.count >= moduleCount, !celebrated {
            celebrated = true
            eng.storyUnderstood()
            toastMsg = "✓ Story completed — nicely done"
        }
    }

    private func modules(for s: StoryDetail) -> [StoryModule] {
        // Stories written since the narrative split carry `whyMatters` as its own
        // field, so `narrative` is the complete storyline. Older stories don't,
        // and for those the legacy split still applies: first paragraph = what,
        // the rest = why.
        let paras = s.narrative.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
        let stored = s.whyMatters ?? ""
        let what = stored.isEmpty ? (paras.first ?? s.narrative) : s.narrative
        let why = stored.isEmpty ? paras.dropFirst().joined(separator: "\n\n") : stored
        var mods: [StoryModule] = [
            .init(id: "what", title: "What happened", icon: "clock", tint: pal.accent,
                  content: .text(what)),
            .init(id: "why", title: "Why it matters", icon: "questionmark.circle", tint: pal.warning,
                  content: .text(why.isEmpty
                      ? "This story links to wider forces — open “The bigger picture” to see which trends it feeds and who is affected."
                      : why)),
        ]
        // Personalized take sits right after the story is told (what -> why),
        // as its own collapsible card, collapsed by default like the rest.
        // Always shown (not just when text exists) — it's how a signed-in,
        // configured reader triggers the on-demand fetch in the first place
        // by opening it; see toggle(_:) and loadPersonalize().
        let personalizeContent: StoryModule.Content
        if let impact = s.impactText, !impact.isEmpty {
            personalizeContent = .forYou(impact)
        } else if api.isGoogleUser && onboarded {
            personalizeContent = forYouChecked ? .forYouEmpty : .forYouLoading
        } else {
            personalizeContent = .forYouLocked
        }
        mods.append(.init(id: "foryou", title: "What this means for you",
                          icon: "scope", tint: pal.accent, content: personalizeContent))
        if let trends = s.trends, !trends.isEmpty {
            mods.append(.init(id: "big", title: "The bigger picture", icon: "chart.line.uptrend.xyaxis",
                              tint: pal.prediction, content: .trends(trends)))
        }
        if let conns = s.connections, !conns.isEmpty {
            mods.append(.init(id: "conn", title: "Hidden connections", icon: "point.3.connected.trianglepath.dotted",
                              tint: pal.ai, content: .connections(conns)))
        }
        mods.append(.init(id: "verify", title: "Claim check", icon: "checkmark.shield",
                          tint: pal.trust, content: .claims(s.claims)))
        if let sources = s.sources, !sources.isEmpty {
            mods.append(.init(id: "src", title: "Sources", icon: "link",
                              tint: pal.text2, content: .sources(sources)))
        }
        return mods
    }
}

// MARK: - Module model + card

struct StoryModule: Identifiable {
    enum Content {
        case text(String)
        case forYou(String)
        case forYouLoading
        case forYouEmpty
        case forYouLocked
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
    @Environment(\.palette) private var pal

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
                        .background(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
                            .fill(module.tint.opacity(0.13)))
                    Text(module.title).font(.subheadline.weight(.semibold))
                    Spacer()
                    Image(systemName: "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(pal.text2)
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
            Text(t).font(.subheadline).foregroundStyle(pal.text2).lineSpacing(3)

        case .forYou(let t):
            Text(t)
                .font(.subheadline)
                .lineSpacing(3)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
                .background(RoundedRectangle(cornerRadius: pal.r(12), style: .continuous)
                    .fill(pal.aiGradient.opacity(0.13))
                    .overlay(RoundedRectangle(cornerRadius: pal.r(12), style: .continuous)
                        .stroke(pal.accent.opacity(0.3), lineWidth: 1)))

        case .forYouLoading:
            HStack(spacing: 10) {
                ProgressView().tint(pal.accent)
                Text("Finding your angle on this story…")
                    .font(.subheadline).foregroundStyle(pal.text2)
            }

        case .forYouEmpty:
            VStack(alignment: .leading, spacing: 4) {
                Text("No personal angle on this one — Descry adds this when a story touches your work, city or interests.")
                    .font(.subheadline).foregroundStyle(pal.text2).lineSpacing(3)
            }

        case .forYouLocked:
            Text("Tell Descry your world once — from Profile — and every story explains what it means for you.")
                .font(.subheadline).foregroundStyle(pal.text2).lineSpacing(3)

        case .trends(let trends):
            VStack(spacing: 10) {
                ForEach(trends, id: \.id) { t in
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(t.name).font(.footnote.weight(.semibold))
                                if t.kind == "micro" {
                                    Chip(text: "EARLY SIGNAL", color: pal.prediction, filled: true)
                                }
                            }
                            Text(t.narrative).font(.caption).foregroundStyle(pal.text2)
                        }
                        Spacer()
                        Sparkline(seed: t.name, color: pal.prediction)
                    }
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
                        .fill(pal.surface2))
                }
            }

        case .connections(let conns):
            VStack(alignment: .leading, spacing: 10) {
                Text("AI-inferred hypotheses — links that aren't obvious but may matter. Treat as leads, not facts.")
                    .font(.caption).foregroundStyle(pal.text2)
                ForEach(conns, id: \.self) { c in
                    VStack(alignment: .leading, spacing: 5) {
                        Label(c.otherTitle, systemImage: "arrow.left.arrow.right")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(pal.prediction)
                        Text(c.chain).font(.caption).foregroundStyle(pal.text2)
                        Text("confidence \(Int(c.confidence * 100))%")
                            .font(.caption2.monospaced()).foregroundStyle(pal.prediction)
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
                        .fill(pal.ai.opacity(0.08))
                        .overlay(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
                            .stroke(pal.ai.opacity(0.22), lineWidth: 1)))
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
                                .font(.caption2).foregroundStyle(pal.text2)
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
                                Circle().fill(pal.accent).frame(width: 7, height: 7)
                                Text(src.title ?? urlStr)
                                    .font(.footnote).foregroundStyle(.white)
                                    .multilineTextAlignment(.leading)
                                Spacer()
                                Text(src.source ?? "")
                                    .font(.caption2.monospaced()).foregroundStyle(pal.text2)
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
        case "corroborated": return pal.trust
        case "disputed": return pal.breaking
        default: return pal.warning
        }
    }
}

// MARK: - Ask AI sheet

struct AskAISheet: View {
    @Environment(\.palette) private var pal

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
                                    HStack { ProgressView().tint(pal.ai); Spacer() }
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
                                        Chip(text: s, color: pal.ai, filled: true)
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
                            .background(RoundedRectangle(cornerRadius: pal.r(12), style: .continuous)
                                .fill(pal.surface2))
                            .onSubmit { send(input) }
                        Button { send(input) } label: {
                            Image(systemName: "arrow.up")
                                .font(.subheadline.weight(.bold))
                                .foregroundStyle(.white)
                                .frame(width: 40, height: 40)
                                .background(Circle().fill(pal.aiGradient))
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
    }

    private func bubble(_ m: Message) -> some View {
        HStack {
            if m.isUser { Spacer(minLength: 40) }
            Text(m.text)
                .font(.subheadline)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
                    .fill(m.isUser ? pal.accent.opacity(0.2) : pal.surface2))
                .overlay(RoundedRectangle(cornerRadius: pal.r(14), style: .continuous)
                    .stroke(m.isUser ? pal.accent.opacity(0.35) : pal.hairline, lineWidth: 1))
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
