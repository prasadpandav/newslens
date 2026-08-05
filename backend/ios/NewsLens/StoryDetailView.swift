import SwiftUI

/// The reader. A single column of serif prose with the story's own section
/// labels, the agreement scale in the bar, real reading progress, and the proof
/// as a pull-up margin rather than a stack of accordions.
///
/// The accordion modules this replaced measured the wrong thing: progress was a
/// count of cards you had clicked open, which is not the same as having read
/// anything. Progress here is scroll position through the article, which is
/// what the design asks for and what the web portal already does.
struct StoryDetailView: View {
    @Environment(\.palette) private var pal
    @Environment(\.dismiss) private var dismiss

    let storyID: String
    @EnvironmentObject var api: APIClient
    @StateObject private var eng = Engagement.shared
    @AppStorage("onboarded") private var onboarded = false
    @State private var story: StoryDetail?
    @State private var error: String?
    /// "What this means for you" hasn't been fetched (nil), is in flight
    /// (true), or came back — possibly empty, which is a real answer ("this
    /// story doesn't touch your interests"), not "not asked yet".
    @State private var forYouLoading = false
    @State private var forYouChecked = false
    @State private var showForYou = false
    @State private var showAsk = false
    @State private var proofOpen = false
    @State private var toastMsg: String?
    /// Furthest point reached, not current position: scrolling back up to
    /// re-read a paragraph is not un-reading it.
    @State private var progress: Double = 0
    @State private var counted = false

    var body: some View {
        ZStack {
            InkBackground()
            VStack(spacing: 0) {
                topBar
                if let s = story {
                    article(s)
                } else if let error {
                    Spacer()
                    ContentUnavailableView("Couldn't load this story",
                                           systemImage: "exclamationmark.triangle",
                                           description: Text(error))
                    Spacer()
                } else {
                    Spacer()
                    ProgressView().tint(pal.accent)
                    Spacer()
                }
            }
        }
        .toolbar(.hidden, for: .navigationBar)
        // The tab bar has to go too: it floats over the bottom of the screen,
        // which is exactly where the proof margin lives — it was covering the
        // panel's own handle and "Open all".
        .toolbar(.hidden, for: .tabBar)
        .toast($toastMsg)
        .sheet(isPresented: $showAsk) {
            AskAISheet(story: story).environmentObject(api).skinned()
        }
        .sheet(isPresented: $showForYou) {
            ForYouSheet(story: story, loading: forYouLoading, checked: forYouChecked)
                .environmentObject(api)
        }
        .task {
            do {
                let s = try await api.fetchStory(id: storyID)
                story = s
                eng.explored(topic: s.topic)
            } catch { self.error = "Server unreachable." }
            await api.sendFeedback(storyID: storyID, action: "open")
        }
    }

    // MARK: - Bar

    /// Back, the verdict, and how long this takes to read — the three things
    /// the mockup's reader bar carries. The headline is not repeated here: it
    /// is four lines high on the page immediately below.
    private var topBar: some View {
        VStack(spacing: 0) {
            HStack {
                Button { dismiss() } label: {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(pal.mute)
                        .frame(width: 44, height: 30, alignment: .leading)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Back")
                Spacer()
                if let s = story {
                    HStack(spacing: 6) {
                        PipScale(credibility: s.credibility, width: 9, height: 4)
                        Text("\(Int(s.credibility.rounded()))")
                            .font(pal.sans(12.5, .medium))
                            .foregroundStyle(pal.credColor(s.credibility))
                    }
                    .accessibilityLabel(AgreementBand.sentence(s.credibility))
                }
                Spacer()
                // The mockup's bar ends in the reading time. On a 390pt phone
                // that slot has to carry save and share as well — they have
                // nowhere else to live now that the navigation bar is hidden —
                // so the reading time moved down into the byline, where it sits
                // beside the dateline and reads better anyway.
                HStack(spacing: 14) {
                    Button {
                        Task {
                            await api.toggleBookmark(storyID: storyID)
                            toastMsg = api.savedStoryIDs.contains(storyID)
                                ? "Saved for later" : "Removed from saved"
                        }
                    } label: {
                        Image(systemName: api.savedStoryIDs.contains(storyID)
                              ? "bookmark.fill" : "bookmark")
                            .font(.system(size: 14))
                            .foregroundStyle(pal.mute)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(api.savedStoryIDs.contains(storyID)
                                        ? "Remove from saved" : "Save for later")
                    .sensoryFeedback(.impact(weight: .light),
                                     trigger: api.savedStoryIDs.contains(storyID))
                    if let s = story {
                        ShareLink(item: api.shareURL("story/\(s.id)"),
                                  subject: Text(s.headline),
                                  message: Text("\(s.headline) — via Descry")) {
                            Image(systemName: "square.and.arrow.up")
                                .font(.system(size: 14))
                                .foregroundStyle(pal.mute)
                        }
                        .accessibilityLabel("Share this story")
                    }
                }
                .frame(width: 62, alignment: .trailing)
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 8)
            Rectangle().fill(pal.hairline).frame(height: 1)
            // Reading progress, 3px, ink on rule — the mockup's bar exactly.
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Rectangle().fill(pal.hairline)
                    Rectangle().fill(pal.text)
                        .frame(width: geo.size.width * progress)
                }
            }
            .frame(height: 3)
            .accessibilityHidden(true)
        }
    }

    // MARK: - Article

    private func article(_ s: StoryDetail) -> some View {
        GeometryReader { outer in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    body(s)
                }
                .padding(.horizontal, 22)
                .padding(.bottom, 40)
                .background(scrollReporter(viewport: outer.size.height))
            }
            .scrollIndicators(.hidden)
            .coordinateSpace(name: Self.space)
        }
        // Ask rides directly above the margin rather than floating over it —
        // as an overlay it sat on top of the panel's own "Open all" control.
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) {
                HStack { Spacer(); askButton }
                ProofMargin(story: s, open: $proofOpen)
            }
        }
    }

    private static let space = "reader"

    /// Reports scroll position as a fraction of the article that has passed the
    /// fold. The denominator subtracts one viewport because the last screenful
    /// needs no scrolling to be read — a story shorter than the screen is
    /// therefore already fully read, which is true.
    private func scrollReporter(viewport: CGFloat) -> some View {
        GeometryReader { geo in
            let frame = geo.frame(in: .named(Self.space))
            let scrollable = frame.height - viewport
            let p = scrollable > 0
                ? min(1, max(0, -frame.minY / scrollable))
                : 1
            Color.clear
                .onChange(of: p) { _, new in
                    if new > progress { progress = new }
                    if progress > 0.9, !counted {
                        counted = true
                        eng.storyUnderstood()
                    }
                }
                .onAppear { if scrollable <= 0 { progress = 1 } }
        }
    }

    @ViewBuilder
    private func body(_ s: StoryDetail) -> some View {
        let beats = s.readerBeats
        let notes = s.proofNotes

        // The first beat's label is the kicker. It is the story's own words for
        // what this section covers ("Why the gap closed"), written per story —
        // which is why it can sit above the headline without repeating it.
        if let first = beats.first {
            Text(first.label)
                .font(pal.mono(12, .medium))
                .kerning(1.68)
                .textCase(.uppercase)
                .foregroundStyle(pal.breaking)
                .padding(.top, 18)
                .padding(.bottom, 10)
        }
        Text(s.headline)
            .font(pal.serif(30, .light))
            .lineSpacing(4)
            .kerning(-0.4)
            .foregroundStyle(pal.text)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.bottom, 12)
        byline(s)
        if let c = s.correction { CorrectionNote(correction: c).padding(.bottom, 14) }
        StoryImage(urlString: s.imageUrl, height: 200)
            .padding(.bottom, 16)

        ForEach(Array(beats.enumerated()), id: \.offset) { idx, beat in
            // Every beat after the first announces itself; the first was already
            // used as the kicker above the headline.
            if idx > 0 {
                Text(beat.label)
                    .font(pal.mono(12, .medium))
                    .kerning(1.68)
                    .textCase(.uppercase)
                    .foregroundStyle(pal.faint)
                    .padding(.top, 26)
                    .padding(.bottom, 10)
            }
            Text(Marked.prose(beat.text, anchors: s.anchors, notes: notes, pal: pal))
                .font(pal.serif(17.5, .light))
                .lineSpacing(7)
                .foregroundStyle(idx == 0 ? pal.text : pal.text2)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, 14)
        }

        forYouCallout(s)

        if let why = s.whyMatters, !why.isEmpty {
            sectionLabel("Why it matters")
            Text(why)
                .font(pal.serif(17, .light))
                .lineSpacing(7)
                .foregroundStyle(pal.text2)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let trends = s.trends, !trends.isEmpty {
            sectionLabel("The bigger picture")
            ForEach(trends, id: \.id) { t in
                VStack(alignment: .leading, spacing: 8) {
                    Text(BriefView.cleanName(t.name))
                        .font(pal.serif(19))
                        .foregroundStyle(pal.text)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(t.narrative)
                        .font(pal.serif(16, .light))
                        .lineSpacing(6)
                        .foregroundStyle(pal.text2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.bottom, 18)
            }
        }
        if let conns = s.connections, !conns.isEmpty {
            sectionLabel("Links worth checking")
            Text("Suggested by the machine, not reported by anyone. Treat these as leads to follow up, not as facts.")
                .font(pal.sans(13.5))
                .lineSpacing(4)
                .foregroundStyle(pal.text3)
                .padding(.bottom, 12)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(conns, id: \.self) { c in
                VStack(alignment: .leading, spacing: 5) {
                    Text(c.otherTitle)
                        .font(pal.sans(15, .medium))
                        .foregroundStyle(pal.text)
                        .fixedSize(horizontal: false, vertical: true)
                    if !c.chain.isEmpty {
                        Text(c.chain)
                            .font(pal.sans(14))
                            .lineSpacing(5)
                            .foregroundStyle(pal.text3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .overlay(alignment: .top) { Rectangle().fill(pal.hairline).frame(height: 1) }
            }
        }

        Text("The storyline and everything called a link are written by a machine from the sources listed in the proof panel. The agreement scale measures how far those sources agree with each other — not whether they are right.")
            .font(pal.sans(13))
            .lineSpacing(4)
            .foregroundStyle(pal.faint)
            .padding(.top, 26)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func byline(_ s: StoryDetail) -> some View {
        HStack(spacing: 7) {
            Text("By the Descry desk")
            Circle().fill(pal.ghost).frame(width: 3, height: 3)
            Text(Date(timeIntervalSince1970: s.createdAt ?? 0)
                .formatted(.dateTime.day().month(.abbreviated).year()))
            Circle().fill(pal.ghost).frame(width: 3, height: 3)
            Text("\(s.readingMinutes) min read")
            if s.isDeveloping, let u = s.updatedAt {
                Circle().fill(pal.ghost).frame(width: 3, height: 3)
                Text("last told \(Ago.short(u))")
            }
            Spacer(minLength: 0)
        }
        .font(pal.sans(13))
        .foregroundStyle(pal.mute)
        .padding(.bottom, 16)
    }

    private func sectionLabel(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Rectangle().fill(pal.hairline).frame(height: 1)
            Text(text)
                .font(pal.mono(12, .medium))
                .kerning(1.68)
                .textCase(.uppercase)
                .foregroundStyle(pal.faint)
                .padding(.top, 16)
                .padding(.bottom, 12)
        }
        .padding(.top, 26)
    }

    /// The way into the dark "Read for you" page. It is a door, not the content:
    /// the personal read is a different kind of writing — about you, not about
    /// the event — and the design gives it its own inverted page for that reason.
    private func forYouCallout(_ s: StoryDetail) -> some View {
        Button {
            showForYou = true
            if (s.impactText ?? "").isEmpty { Task { await loadPersonalize() } }
        } label: {
            HStack(spacing: 0) {
                Rectangle().fill(pal.sandEdge).frame(width: 2)
                VStack(alignment: .leading, spacing: 6) {
                    Text("Why this matters to you")
                        .font(pal.serif(17, .medium))
                        .foregroundStyle(pal.sandInk)
                    Text(preview(s))
                        .font(pal.sans(14.5))
                        .lineSpacing(5)
                        .foregroundStyle(pal.sandText)
                        .multilineTextAlignment(.leading)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 13).padding(.vertical, 12)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(pal.sandInk)
                    .padding(.trailing, 13)
            }
            .background(pal.sand)
            .clipShape(UnevenRoundedRectangle(bottomTrailingRadius: pal.r(5),
                                              topTrailingRadius: pal.r(5)))
        }
        .buttonStyle(.plain)
        .padding(.top, 10)
    }

    private func preview(_ s: StoryDetail) -> String {
        if let t = s.impactText, !t.isEmpty { return t }
        if api.isGoogleUser && onboarded { return "Read this one against your work, your city and what you follow." }
        return "Tell Descry your world once and every story explains what it means for you."
    }

    private var askButton: some View {
        Button { showAsk = true } label: {
            Image(systemName: "sparkle")
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(pal.ink)
                .frame(width: 46, height: 46)
                .background(Circle().fill(pal.text))
        }
        .buttonStyle(.plain)
        .padding(.trailing, 20)
        .padding(.bottom, 12)
        .accessibilityLabel("Ask about this story")
    }

    /// Fires the first time the reader opens "Why this matters to you" and
    /// nothing was already cached from a previous open. Guarded so re-opening
    /// the page doesn't refetch.
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
}

// MARK: - Marking a claim to its line

enum Marked {
    /// Prose with the sentences the writer produced for a checked claim tinted
    /// and numbered to their note in the margin.
    ///
    /// The anchor is an exact string match, never a fuzzy one: `anchors` carries
    /// the sentence the writer actually wrote for that claim, verified
    /// server-side to occur verbatim in this prose. A claim the model
    /// paraphrased is simply not marked, which is the right failure — tinting a
    /// sentence that did not carry the claim would put our verdict on someone
    /// else's words.
    static func prose(_ text: String, anchors: [StoryDetail.Anchor]?,
                      notes: [ProofNote], pal: Palette) -> AttributedString {
        var out = AttributedString(text)
        guard let anchors, !anchors.isEmpty, !notes.isEmpty else { return out }
        // Longest first, so a short quote can't shadow a longer one it sits in.
        for anchor in anchors.sorted(by: { $0.quote.count > $1.quote.count }) {
            let quote = anchor.quote.trimmingCharacters(in: .whitespacesAndNewlines)
            guard quote.count >= 24, anchor.claim >= 0, anchor.claim < notes.count,
                  let range = out.range(of: quote) else { continue }
            let note = notes[anchor.claim]
            out[range].backgroundColor = note.tick(pal).opacity(0.16)
            out[range].underlineStyle = .single

            // The marker points at the note's number in the margin.
            var marker = AttributedString(" \(anchor.claim + 1)")
            marker.font = pal.mono(12, .medium)
            marker.baselineOffset = 7
            marker.foregroundColor = note.color(pal)
            out.insert(marker, at: range.upperBound)
        }
        return out
    }
}

// MARK: - The margin, as a pull-up

/// "Proof for this page · 2 notes". Collapsed it is a handle and a count;
/// dragged or tapped up it becomes the full margin — every checked claim, then
/// where the story came from.
///
/// Built as a bottom inset rather than a `.sheet` because it belongs to the
/// article: a sheet survives the push back to the feed and has its own idea of
/// when it should be dismissed, and neither is what a margin does.
struct ProofMargin: View {
    let story: StoryDetail
    @Binding var open: Bool
    @Environment(\.palette) private var pal
    @State private var drag: CGFloat = 0

    private var notes: [ProofNote] { story.proofNotes }

    var body: some View {
        VStack(spacing: 0) {
            handle
            if open { detail }
        }
        .background(pal.surface2)
        .overlay(alignment: .top) { Rectangle().fill(pal.hairline2).frame(height: 1) }
        .clipShape(UnevenRoundedRectangle(topLeadingRadius: pal.r(16),
                                          topTrailingRadius: pal.r(16)))
        .offset(y: max(0, drag))
        .gesture(
            DragGesture()
                .onChanged { drag = open ? $0.translation.height : min(0, $0.translation.height) }
                .onEnded { value in
                    withAnimation(BL.spring) {
                        if value.translation.height < -30 { open = true }
                        if value.translation.height > 40 { open = false }
                        drag = 0
                    }
                }
        )
    }

    private var handle: some View {
        Button { withAnimation(BL.spring) { open.toggle() } } label: {
            VStack(spacing: 10) {
                Capsule().fill(pal.hairline2).frame(width: 34, height: 4)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("Proof for this page")
                        .font(pal.serif(17, .medium))
                        .foregroundStyle(pal.text)
                    Text(countLabel)
                        .font(pal.mono(12.5))
                        .foregroundStyle(pal.faint)
                    Spacer(minLength: 0)
                    Text(open ? "Close" : "Open all")
                        .font(pal.sans(14))
                        .foregroundStyle(pal.accent)
                }
            }
            .padding(.horizontal, 22)
            .padding(.top, 12)
            .padding(.bottom, open ? 12 : 16)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Proof for this page, \(countLabel)")
        .accessibilityHint(open ? "Collapses the proof panel" : "Opens the proof panel")
    }

    private var countLabel: String {
        notes.isEmpty ? "nothing checked yet"
            : "\(notes.count) note\(notes.count == 1 ? "" : "s")"
    }

    private var detail: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if notes.isEmpty {
                    Text("No fact in this story has been checked against another source yet. That is a gap in what we have done, not a verdict on the story.")
                        .font(pal.sans(14))
                        .lineSpacing(5)
                        .foregroundStyle(pal.text3)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    ForEach(Array(notes.enumerated()), id: \.element.id) { idx, note in
                        proofNote(idx: idx, note: note)
                    }
                }
                if let sources = story.sources, !sources.isEmpty {
                    Rectangle().fill(pal.hairline).frame(height: 1).padding(.vertical, 16)
                    Text("Where this came from")
                        .font(pal.mono(12, .medium))
                        .kerning(1.68)
                        .textCase(.uppercase)
                        .foregroundStyle(pal.faint)
                        .padding(.bottom, 10)
                    ForEach(sources, id: \.self) { src in
                        sourceRow(src)
                    }
                }
            }
            .padding(.horizontal, 22)
            .padding(.bottom, 24)
        }
        .scrollIndicators(.hidden)
        .frame(maxHeight: 380)
    }

    private func proofNote(idx: Int, note: ProofNote) -> some View {
        HStack(alignment: .top, spacing: 9) {
            // The tick is the same colour as the mark on the line it belongs to.
            Rectangle().fill(note.tick(pal))
                .frame(width: 12, height: 1)
                .padding(.top, 9)
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Text("\(idx + 1)")
                        .font(pal.mono(12, .medium))
                        .foregroundStyle(pal.faint)
                    Text(note.label)
                        .font(pal.mono(12, .medium))
                        .kerning(0.6)
                        .textCase(.uppercase)
                        .foregroundStyle(note.color(pal))
                }
                Text(note.claim)
                    .font(pal.sans(14))
                    .lineSpacing(4)
                    .foregroundStyle(pal.text2)
                    .fixedSize(horizontal: false, vertical: true)
                if !note.note.isEmpty {
                    Text(note.note)
                        .font(pal.mono(12.5))
                        .lineSpacing(3)
                        .foregroundStyle(pal.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.bottom, 20)
    }

    @ViewBuilder
    private func sourceRow(_ src: StoryDetail.Source) -> some View {
        if let urlStr = src.url, let url = URL(string: urlStr) {
            Link(destination: url) {
                HStack(alignment: .top, spacing: 9) {
                    Circle().fill(pal.ghost).frame(width: 4, height: 4).padding(.top, 6)
                    Text(src.title ?? urlStr)
                        .font(pal.sans(14))
                        .foregroundStyle(pal.accent)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 6)
                    Text(src.source ?? "")
                        .font(pal.mono(12))
                        .foregroundStyle(pal.faint)
                }
                .padding(.vertical, 6)
            }
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
                                    HStack { ProgressView().tint(pal.accent); Spacer() }
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
                                        Chip(text: s)
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                            .padding(.horizontal, 16)
                        }
                        .padding(.bottom, 8)
                    }
                    HStack(spacing: 8) {
                        TextField("Ask about this story…", text: $input)
                            .textFieldStyle(.plain)
                            .font(pal.sans(14))
                            .padding(.horizontal, 14).padding(.vertical, 11)
                            .background(RoundedRectangle(cornerRadius: pal.r(10), style: .continuous)
                                .fill(pal.surface2))
                            .onSubmit { send(input) }
                        Button { send(input) } label: {
                            Image(systemName: "arrow.up")
                                .font(.subheadline.weight(.bold))
                                .foregroundStyle(pal.ink)
                                .frame(width: 40, height: 40)
                                .background(Circle().fill(pal.text))
                        }
                        .buttonStyle(.plain)
                        .disabled(input.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    .padding(12)
                }
            }
            .navigationTitle("Ask")
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
                .font(pal.sans(14))
                .lineSpacing(3)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: pal.r(12), style: .continuous)
                    .fill(m.isUser ? pal.surface2 : pal.ink2))
                .overlay(RoundedRectangle(cornerRadius: pal.r(12), style: .continuous)
                    .stroke(pal.hairline, lineWidth: 1))
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
