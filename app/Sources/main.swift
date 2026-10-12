// Filmstamp.app — a SwiftUI front end for the filmstamp command line tool.
//
// The app contains no stamping logic. It runs the repo's `filmstamp` launcher (which uses the
// repo's own virtualenv), so the CLI and the app can never disagree, and shows what the tool
// reports.
//
//   Stamp          type R## or drop the afilm zip / roll folder. The check (dry run) runs by
//                  itself; Write stays disabled until a check has passed for exactly these
//                  inputs, and disables again the moment an input changes.
//   Contact sheet  R##.pdf for the binder.
//   Settings       roll-profile.toml as text, validated by the tool before it is saved.

import SwiftUI
import AppKit
import WebKit
import PDFKit

// MARK: - Running the tool

struct RunResult {
    var out: String
    var err: String
    var status: Int32
}

private final class Collector: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()
    func add(_ d: Data) { lock.lock(); data.append(d); lock.unlock() }
    var text: String { lock.lock(); defer { lock.unlock() }; return String(decoding: data, as: UTF8.self) }
}

enum Tool {
    static let defaultsKey = "repoPath"
    // A GUI app starts with a bare PATH; the tool needs exiftool (Homebrew) and sips.
    static let searchPath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    static var repo: URL {
        get {
            if let p = ProcessInfo.processInfo.environment["FILMSTAMP_REPO"] { return URL(fileURLWithPath: p) }
            if let p = UserDefaults.standard.string(forKey: defaultsKey) { return URL(fileURLWithPath: p) }
            return FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Documents/GitHub/filmstamp")
        }
        set { UserDefaults.standard.set(newValue.path, forKey: defaultsKey) }
    }
    static var launcher: URL { repo.appendingPathComponent("filmstamp") }
    static var profile: URL { repo.appendingPathComponent("roll-profile.toml") }

    /// What is missing, in words the operator can act on. Empty means ready.
    static func problems() -> [String] {
        let fm = FileManager.default
        var found: [String] = []
        if !fm.isExecutableFile(atPath: launcher.path) {
            found.append("The filmstamp tool was not found at \(repo.path). Use “Locate filmstamp folder…”.")
        } else if !fm.isExecutableFile(atPath: repo.appendingPathComponent(".venv/bin/python").path) {
            found.append("The tool's virtualenv is missing at \(repo.path)/.venv — create it in that folder with: python3 -m venv .venv && .venv/bin/pip install jinja2 timezonefinder")
        }
        let exiftool = ["/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool", "/usr/bin/exiftool"]
        if !exiftool.contains(where: { fm.isExecutableFile(atPath: $0) }) {
            found.append("ExifTool is not installed. In Terminal: brew install exiftool")
        }
        return found
    }

    static func run(_ args: [String]) async -> RunResult {
        await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = Process()
                p.executableURL = launcher
                p.arguments = args
                // The tool looks for ./roll-profile.toml first; pin that to the repo's own, so
                // the profile never depends on where the app happened to be started.
                p.currentDirectoryURL = repo
                var env = ProcessInfo.processInfo.environment
                env["PATH"] = searchPath
                env["PYTHONUNBUFFERED"] = "1"
                p.environment = env
                let outPipe = Pipe(), errPipe = Pipe()
                p.standardOutput = outPipe
                p.standardError = errPipe
                let out = Collector(), err = Collector()
                do { try p.run() } catch {
                    cont.resume(returning: RunResult(out: "", err: "Could not start \(launcher.path): \(error.localizedDescription)", status: -1))
                    return
                }
                // The parent must close its copy of the write ends, or the reads never see EOF.
                try? outPipe.fileHandleForWriting.close()
                try? errPipe.fileHandleForWriting.close()
                // Each pipe is read to EOF on its own thread (so neither can fill up and stall
                // the tool), and only then do we collect the exit status.
                let group = DispatchGroup()
                for (pipe, sink) in [(outPipe, out), (errPipe, err)] {
                    group.enter()
                    DispatchQueue.global(qos: .userInitiated).async {
                        sink.add(pipe.fileHandleForReading.readDataToEndOfFile())
                        group.leave()
                    }
                }
                group.wait()
                p.waitUntilExit()
                cont.resume(returning: RunResult(out: out.text, err: err.text, status: p.terminationStatus))
            }
        }
    }
}

// MARK: - Small helpers

struct Resolved {
    var roll: String
    var export: String?
    var scanFolder: String?
    var errors: [String]
}

func firstMatch(_ pattern: String, in text: String, options: NSRegularExpression.Options = [.anchorsMatchLines]) -> String? {
    guard let re = try? NSRegularExpression(pattern: pattern, options: options),
          let m = re.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
          m.numberOfRanges > 1, let r = Range(m.range(at: 1), in: text) else { return nil }
    return String(text[r])
}

func matches(_ pattern: String, in text: String) -> [String] {
    guard let re = try? NSRegularExpression(pattern: pattern, options: [.anchorsMatchLines]) else { return [] }
    return re.matches(in: text, range: NSRange(text.startIndex..., in: text)).compactMap {
        guard $0.numberOfRanges > 1, let r = Range($0.range(at: 1), in: text) else { return nil }
        return String(text[r])
    }
}

func isShorthand(_ s: String) -> Bool {
    s.range(of: #"^[Rr]\d+$"#, options: .regularExpression) != nil
}

struct LensOption: Hashable { let key: String; let model: String }

func loadLenses() async -> [LensOption] {
    let r = await Tool.run(["lenses"])
    guard r.status == 0, let data = r.out.data(using: .utf8),
          let rows = try? JSONSerialization.jsonObject(with: data) as? [[String: String]] else { return [] }
    return rows.compactMap { row in
        guard let k = row["key"] else { return nil }
        return LensOption(key: k, model: row["model"] ?? k)
    }
}

func resolveRoll(_ roll: String) async -> Resolved? {
    let r = await Tool.run(["resolve", roll])
    guard r.status == 0, let data = r.out.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
    return Resolved(
        roll: obj["roll"] as? String ?? roll,
        export: obj["export"] as? String,
        scanFolder: obj["scan_folder"] as? String,
        errors: obj["errors"] as? [String] ?? []
    )
}

/// Roll shorthand and (for a dropped zip or folder) which path was dropped.
enum Dropped {
    case export(roll: String, path: String)
    case folder(roll: String, path: String)
}

func classifyDrop(_ url: URL) -> Dropped? {
    let name = url.lastPathComponent
    var isDir: ObjCBool = false
    FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir)
    if isDir.boolValue, let n = firstMatch(#"^R(\d+)"#, in: name) { return .folder(roll: "R" + n, path: url.path) }
    if url.pathExtension.lowercased() == "zip", let n = firstMatch(#"^R(\d+)"#, in: name) {
        return .export(roll: "R" + n, path: url.path)
    }
    return nil
}

func loadDroppedURL(_ providers: [NSItemProvider], _ handle: @escaping (URL) -> Void) -> Bool {
    guard let p = providers.first(where: { $0.canLoadObject(ofClass: URL.self) }) else { return false }
    _ = p.loadObject(ofClass: URL.self) { url, _ in
        if let url { DispatchQueue.main.async { handle(url) } }
    }
    return true
}

func chooseRepoFolder() {
    let panel = NSOpenPanel()
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.message = "Choose the filmstamp folder (the one containing the “filmstamp” tool)"
    if panel.runModal() == .OK, let url = panel.url { Tool.repo = url }
}

// MARK: - View state without @State
//
// With only the Command Line Tools installed, the SwiftUI macro plugin that expands @State is
// missing, so views keep their local state in a tiny ObservableObject instead.

final class Local<T>: ObservableObject {
    @Published var value: T
    init(_ value: T) { self.value = value }
}

// MARK: - Embedded views

struct WebPage: NSViewRepresentable {
    let url: URL
    let token: Int

    func makeCoordinator() -> Coordinator { Coordinator() }
    final class Coordinator { var lastURL: URL?; var lastToken = -1 }

    func makeNSView(context: Context) -> WKWebView { WKWebView(frame: .zero) }
    func updateNSView(_ view: WKWebView, context: Context) {
        guard context.coordinator.lastURL != url || context.coordinator.lastToken != token else { return }
        context.coordinator.lastURL = url
        context.coordinator.lastToken = token
        view.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
    }
}

struct PDFPage: NSViewRepresentable {
    let url: URL
    let token: Int

    func makeCoordinator() -> Coordinator { Coordinator() }
    final class Coordinator { var lastToken = -1 }

    func makeNSView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        return v
    }
    func updateNSView(_ view: PDFView, context: Context) {
        guard context.coordinator.lastToken != token else { return }
        context.coordinator.lastToken = token
        view.document = PDFDocument(url: url)
    }
}

struct Banner: View {
    enum Kind { case ok, warn, error, info }
    let kind: Kind
    let text: String
    var body: some View {
        let color: Color = { switch kind { case .ok: return .green; case .warn: return .orange; case .error: return .red; case .info: return .secondary } }()
        Text(text)
            .font(.callout)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(color.opacity(0.14))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(color.opacity(0.5)))
            .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

struct DropZone: View {
    let label: String
    @Binding var targeted: Bool
    var body: some View {
        Text(label)
            .font(.callout)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, minHeight: 44)
            .overlay(RoundedRectangle(cornerRadius: 8)
                .strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [5]))
                .foregroundStyle(targeted ? Color.accentColor : Color.secondary.opacity(0.5)))
    }
}

struct EnvironmentBanner: View {
    @Binding var problems: [String]
    var body: some View {
        if !problems.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(problems, id: \.self) { Banner(kind: .error, text: $0) }
                HStack {
                    Button("Locate filmstamp folder…") { chooseRepoFolder(); problems = Tool.problems() }
                    Button("Check again") { problems = Tool.problems() }
                }
            }
        }
    }
}

// MARK: - Stamp

@MainActor
final class StampModel: ObservableObject {
    @Published var roll = ""
    @Published var resolved: Resolved?
    @Published var exportOverride: String?
    @Published var folderOverride: String?
    @Published var busy: String?
    @Published var checkOK = false
    @Published var checkMessage: Banner.Kind = .info
    @Published var checkText = "Type a roll number like R49, or drop the afilm zip or the roll folder."
    @Published var written = false
    @Published var outcome: (kind: Banner.Kind, text: String)?
    @Published var reviewURL: URL?
    @Published var reviewToken = 0
    @Published var log = ""
    /// Frames the afilm log has no lens for, and the lens picked for each (key). The tool
    /// refuses to write while any of them has no pick, so there is no silent default.
    @Published var lensFrames: [Int] = []
    @Published var lensChoices: [Int: String] = [:]
    @Published var lensOptions: [LensOption] = []

    private var checkedKey: String?
    private var overrideRoll: String?
    private var task: Task<Void, Never>?

    var exportPath: String? { exportOverride ?? resolved?.export }
    var folderPath: String? { folderOverride ?? resolved?.scanFolder }
    var currentKey: String { "\(exportPath ?? "")|\(folderPath ?? "")|\(lensArgs.joined(separator: " "))" }
    /// --lens arguments for the picks made so far, one per lens.
    var lensArgs: [String] {
        let byLens = Dictionary(grouping: lensChoices.keys.sorted(), by: { lensChoices[$0]! })
        return byLens.keys.sorted().flatMap { key in
            ["--lens", byLens[key]!.map(String.init).joined(separator: ",") + "=" + key]
        }
    }
    var lensUnpicked: [Int] { lensFrames.filter { lensChoices[$0] == nil } }

    func pickLens(_ key: String?, frames: [Int]) {
        for f in frames { lensChoices[f] = key }
        guard busy == nil, exportPath != nil, folderPath != nil else { return }
        Task { await check() }   // the review and the write gate must reflect the pick
    }
    var rollID: String { roll.trimmingCharacters(in: .whitespaces).uppercased() }

    /// The write gate. Not a checkbox: it is derived from what was actually checked.
    var canWrite: Bool { busy == nil && checkOK && checkedKey == currentKey && !written && lensUnpicked.isEmpty }

    func rollEdited() {
        // A drop sets the roll programmatically and has already started its own check.
        if rollID == overrideRoll { return }
        exportOverride = nil; folderOverride = nil; overrideRoll = nil
        reset()
        task?.cancel()
        guard isShorthand(rollID) else { resolved = nil; return }
        task = Task {
            try? await Task.sleep(nanoseconds: 450_000_000)
            if Task.isCancelled { return }
            await refresh()
        }
    }

    func apply(_ dropped: Dropped) {
        guard busy == nil else { return }   // a tool run is in flight; ignore the drop
        switch dropped {
        case .export(let r, let p): roll = r; exportOverride = p; overrideRoll = r
        case .folder(let r, let p): roll = r; folderOverride = p; overrideRoll = r
        }
        reset()
        task?.cancel()
        task = Task { await refresh() }
    }

    private func reset() {
        checkOK = false; checkedKey = nil; written = false; outcome = nil
        lensFrames = []; lensChoices = [:]
        reviewURL = nil      // never show a previous roll's review under a new roll
        checkMessage = .info
        checkText = isShorthand(rollID) ? "Looking for the files…" : "Type a roll number like R49, or drop the afilm zip or the roll folder."
    }

    func refresh() async {
        guard isShorthand(rollID), busy == nil else { return }   // one tool run at a time
        busy = "Looking for the files…"
        resolved = await resolveRoll(rollID)
        busy = nil
        guard let r = resolved else {
            checkMessage = .error; checkText = "Could not run the filmstamp tool."; return
        }
        if exportPath == nil || folderPath == nil {
            checkMessage = .error
            checkText = r.errors.joined(separator: "\n")
            return
        }
        await check()
    }

    private var baseArgs: [String] { [exportPath ?? "", folderPath ?? ""] + lensArgs }

    /// The dry run. There is no "dry run" button: it is the only possible first step.
    func check() async {
        guard exportPath != nil, folderPath != nil, busy == nil else { return }
        let key = currentKey
        busy = "Checking (nothing is written to the scans)…"
        checkOK = false; written = false; outcome = nil; reviewURL = nil
        let r = await Tool.run(baseArgs + ["--no-open"])
        busy = nil
        guard key == currentKey else { return }   // inputs changed while it ran
        log = r.out + (r.err.isEmpty ? "" : "\n" + r.err)
        if r.status == 0 {
            checkOK = true
            checkedKey = key
            let warnings = firstMatch(#"^(\d+) warning\(s\):"#, in: r.out) ?? "0"
            checkMessage = warnings == "0" ? .ok : .warn
            let blank = matches(#"Frame (\d+): no lens recorded"#, in: r.out).compactMap(Int.init)
            if !blank.isEmpty {
                lensFrames = Array(Set(lensFrames).union(blank)).sorted()
                if lensOptions.isEmpty { lensOptions = await loadLenses() }
            }
            checkText = lensUnpicked.isEmpty
                ? "Check passed — \(warnings) warning(s). Compare each scan with its phone snap, then Write."
                : "No lens is recorded for frame(s) \(lensUnpicked.map(String.init).joined(separator: ", ")). Pick the lens below, then Write."
            if !lensUnpicked.isEmpty { checkMessage = .warn }
            showReview(from: r.out)
        } else {
            checkMessage = .error
            checkText = r.err.trimmingCharacters(in: .whitespacesAndNewlines)
            if checkText.isEmpty { checkText = "The check failed (exit \(r.status))." }
        }
    }

    private func showReview(from out: String) {
        if let path = firstMatch(#"^Wrote (.+\.html)$"#, in: out) {
            reviewURL = URL(fileURLWithPath: path)
        } else if out.contains("No review page produced"), let folder = folderPath {
            // the roll already has a record of a real write; show that one
            let name = "\(rollID).html"
            reviewURL = URL(fileURLWithPath: folder).appendingPathComponent(name)
            checkText += " This roll already has a record of an earlier write, shown here unchanged."
            if out.contains("is OUT OF DATE") {
                checkMessage = .warn
                checkText += " That record is OUT OF DATE: a fresh check gives different values (e.g. the lens). Re-stamp the roll to update the files and the record."
            }
        }
        reviewToken += 1
    }

    func write(force: Bool) async {
        guard busy == nil, canWrite || (force && checkOK) else { return }
        busy = "Writing and verifying every file…"
        var args = baseArgs + ["--write", "--no-open"]
        if force { args.append("--force") }
        let r = await Tool.run(args)
        busy = nil
        log = r.out + (r.err.isEmpty ? "" : "\n" + r.err)
        written = true
        if r.status == 3 {
            // Exit 3: every file was written and verified; only the export archive copy failed.
            outcome = (.warn, "Written and verified — but the export could not be archived. Do not re-stamp; the files are correct.\n" + r.err.trimmingCharacters(in: .whitespacesAndNewlines))
        } else if r.status == 0 {
            let lines = r.out.split(separator: "\n").map(String.init)
                .filter { $0.hasPrefix("Wrote and verified") || $0.hasPrefix("Archived") || $0.hasPrefix("Already archived") || $0.hasPrefix("Skipped") }
            outcome = (.ok, "Written and verified.\n" + lines.joined(separator: "\n"))
        } else {
            outcome = (.error, "NOT everything was written and verified. Nothing is left unchecked:\n" + r.err)
        }
        showReview(from: r.out)
    }
}

struct StampView: View {
    @StateObject private var m = StampModel()
    @StateObject private var targeted = Local(false)
    @StateObject private var problems = Local(Tool.problems())
    @StateObject private var showForce = Local(false)
    @StateObject private var forceConfirm = Local("")

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            EnvironmentBanner(problems: $problems.value)
            HStack {
                Text("Roll").font(.headline)
                TextField("R49", text: $m.roll)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 120)
                    .onChange(of: m.roll) { m.rollEdited() }
                    .disabled(m.busy != nil)
                DropZone(label: "or drop the afilm zip / the roll folder here", targeted: $targeted.value)
                    .onDrop(of: [.fileURL], isTargeted: $targeted.value) { providers in
                        guard m.busy == nil else { return false }
                        return loadDroppedURL(providers) { url in if let d = classifyDrop(url) { m.apply(d) } }
                    }
            }
            VStack(alignment: .leading, spacing: 2) {
                pathLine("afilm export", m.exportPath)
                pathLine("scan folder", m.folderPath)
            }
            if let busy = m.busy { ProgressView(busy).controlSize(.small) }
            Banner(kind: m.checkMessage, text: m.checkText)
            if !m.lensFrames.isEmpty { lensPicker }

            if let url = m.reviewURL {
                WebPage(url: url, token: m.reviewToken)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
            } else {
                Spacer()
            }

            if let o = m.outcome { Banner(kind: o.kind, text: o.text) }

            HStack {
                Button(action: { Task { await m.write(force: false) } }) {
                    Label("Write", systemImage: "square.and.pencil").frame(minWidth: 90)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!m.canWrite || !problems.value.isEmpty)
                .help("Enabled only after a check has passed for exactly these inputs")
                Button("Check again") { Task { await m.check() } }
                    .disabled(m.busy != nil || m.exportPath == nil || m.folderPath == nil)
                Spacer()
                DisclosureGroup("Re-stamp files that are already stamped…", isExpanded: $showForce.value) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("This replaces metadata that is already in the files, including values edited since. Type the roll number to confirm.")
                            .font(.caption).foregroundStyle(.secondary)
                        HStack {
                            TextField(m.rollID, text: $forceConfirm.value).textFieldStyle(.roundedBorder).frame(width: 90)
                            Button("Re-stamp (--force)", role: .destructive) {
                                forceConfirm.value = ""; showForce.value = false
                                Task { await m.write(force: true) }
                            }
                            .disabled(forceConfirm.value.uppercased() != m.rollID || !(m.checkOK) || m.busy != nil)
                        }
                    }
                }
                .frame(width: 330)
            }
            DisclosureGroup("Tool output") {
                ScrollView { Text(m.log).font(.system(.caption, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                    .frame(height: 110)
            }
        }
        .padding(14)
    }

    /// Shown only when the afilm log has no lens for some frames.
    private var lensPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Lens for frame(s) \(m.lensFrames.map(String.init).joined(separator: ", "))").font(.headline)
                Picker("", selection: Binding<String?>(
                    get: {
                        let keys = Set(m.lensFrames.map { m.lensChoices[$0] })
                        return keys.count == 1 ? keys.first! : nil
                    },
                    set: { m.pickLens($0, frames: m.lensFrames) }
                )) {
                    Text("Choose a lens…").tag(String?.none)
                    ForEach(m.lensOptions, id: \.key) { Text($0.model).tag(String?.some($0.key)) }
                }
                .labelsHidden().frame(width: 280).disabled(m.busy != nil)
            }
            if m.lensFrames.count > 1 {
                DisclosureGroup("Different lens for some frames") {
                    ForEach(m.lensFrames, id: \.self) { f in
                        HStack {
                            Text("Frame \(f)").frame(width: 70, alignment: .leading)
                            Picker("", selection: Binding<String?>(
                                get: { m.lensChoices[f] },
                                set: { m.pickLens($0, frames: [f]) }
                            )) {
                                Text("Choose a lens…").tag(String?.none)
                                ForEach(m.lensOptions, id: \.key) { Text($0.model).tag(String?.some($0.key)) }
                            }
                            .labelsHidden().frame(width: 280).disabled(m.busy != nil)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private func pathLine(_ label: String, _ path: String?) -> some View {
        HStack(spacing: 6) {
            Image(systemName: path == nil ? "xmark.circle" : "checkmark.circle")
                .foregroundStyle(path == nil ? Color.secondary : Color.green)
            Text(label).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            Text(path ?? "—").font(.system(.callout, design: .monospaced)).lineLimit(1).truncationMode(.middle)
        }
    }
}

// MARK: - Contact sheet

@MainActor
final class SheetModel: ObservableObject {
    @Published var roll = ""
    @Published var subtitle = ""
    @Published var folder: String?
    @Published var busy: String?
    @Published var message: (kind: Banner.Kind, text: String) = (.info, "Type a roll number like R49, or drop the roll folder.")
    @Published var pdfURL: URL?
    @Published var token = 0
    @Published var canReplace = false

    private var task: Task<Void, Never>?
    var rollID: String { roll.trimmingCharacters(in: .whitespaces).uppercased() }

    func rollEdited() {
        folder = nil; pdfURL = nil; canReplace = false
        task?.cancel()
        guard isShorthand(rollID) else { message = (.info, "Type a roll number like R49, or drop the roll folder."); return }
        task = Task {
            try? await Task.sleep(nanoseconds: 450_000_000)
            if Task.isCancelled { return }
            let asked = rollID
            let r = await resolveRoll(asked)
            if Task.isCancelled || asked != rollID { return }   // superseded by a newer roll
            folder = r?.scanFolder
            message = folder == nil ? (.error, (r?.errors.last ?? "Roll folder not found.")) : (.info, "Ready.")
        }
    }

    func make(replace: Bool) async {
        guard folder != nil else { return }
        busy = "Making the contact sheet…"
        var args = ["sheet", rollID, "--no-open"]
        if !subtitle.trimmingCharacters(in: .whitespaces).isEmpty { args += ["--subtitle", subtitle] }
        if replace { args.append("--force") }
        let r = await Tool.run(args)
        busy = nil
        canReplace = false
        if r.status == 0, let path = firstMatch(#"^Wrote (.+\.pdf)$"#, in: r.out) {
            pdfURL = URL(fileURLWithPath: path); token += 1
            let warnings = r.err.split(separator: "\n").filter { $0.hasPrefix("WARNING") }.joined(separator: "\n")
            let notes = r.out.split(separator: "\n").filter { $0.hasPrefix("NOTE") || $0.hasPrefix("Gap") || $0.hasPrefix("Filled") }.joined(separator: "\n")
            let extra = [warnings, notes].filter { !$0.isEmpty }.joined(separator: "\n")
            message = (warnings.isEmpty ? .ok : .warn, "Wrote \(path)" + (extra.isEmpty ? "" : "\n" + extra))
        } else {
            let err = r.err.trimmingCharacters(in: .whitespacesAndNewlines)
            canReplace = err.contains("already exists")
            message = (.error, err.isEmpty ? "The tool failed (exit \(r.status))." : err)
        }
    }

    func apply(_ d: Dropped) {
        switch d { case .export(let r, _), .folder(let r, _): roll = r }
        rollEdited()
    }
}

struct SheetView: View {
    @StateObject private var m = SheetModel()
    @StateObject private var targeted = Local(false)
    @StateObject private var problems = Local(Tool.problems())
    @StateObject private var confirmReplace = Local(false)

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            EnvironmentBanner(problems: $problems.value)
            HStack {
                Text("Roll").font(.headline)
                TextField("R49", text: $m.roll).textFieldStyle(.roundedBorder).frame(width: 120)
                    .onChange(of: m.roll) { m.rollEdited() }
                Text("Subtitle").font(.headline)
                TextField("optional, e.g. Point Pelee & Niagara", text: $m.subtitle).textFieldStyle(.roundedBorder)
            }
            DropZone(label: "or drop the roll folder here", targeted: $targeted.value)
                .onDrop(of: [.fileURL], isTargeted: $targeted.value) { providers in
                    loadDroppedURL(providers) { url in if let d = classifyDrop(url) { m.apply(d) } }
                }
            if let busy = m.busy { ProgressView(busy).controlSize(.small) }
            Banner(kind: m.message.kind, text: m.message.text)
            if let url = m.pdfURL {
                PDFPage(url: url, token: m.token)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
            } else { Spacer() }
            HStack {
                Button("Make contact sheet") { Task { await m.make(replace: false) } }
                    .buttonStyle(.borderedProminent)
                    .disabled(m.folder == nil || m.busy != nil || !problems.value.isEmpty)
                if m.canReplace {
                    Button("Replace the existing sheet…", role: .destructive) { confirmReplace.value = true }
                }
                Spacer()
                if let url = m.pdfURL {
                    Button("Open in Preview") { NSWorkspace.shared.open(url) }
                    Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                }
            }
        }
        .padding(14)
        .confirmationDialog("Replace the existing \(m.rollID).pdf?", isPresented: $confirmReplace.value) {
            Button("Replace", role: .destructive) { Task { await m.make(replace: true) } }
        } message: { Text("The old sheet is overwritten.") }
    }
}

// MARK: - Settings

@MainActor
final class SettingsModel: ObservableObject {
    @Published var text = ""
    @Published var original = ""
    @Published var message: (kind: Banner.Kind, text: String)?
    @Published var busy = false

    var dirty: Bool { text != original }

    func load() {
        do {
            original = try String(contentsOf: Tool.profile, encoding: .utf8)
            text = original
            message = nil
        } catch {
            message = (.error, "Could not read \(Tool.profile.path): \(error.localizedDescription)")
        }
    }

    /// Validated by the tool itself, on a temp copy, so a broken profile is never saved.
    func validate(_ candidate: String? = nil) async -> Bool {
        let candidate = candidate ?? text
        busy = true; defer { busy = false }
        let tmp = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("filmstamp-profile-check-\(UUID().uuidString).toml")
        do { try candidate.write(to: tmp, atomically: true, encoding: .utf8) }
        catch { message = (.error, "Could not write a temp file: \(error.localizedDescription)"); return false }
        let r = await Tool.run(["check-profile", tmp.path])
        try? FileManager.default.removeItem(at: tmp)
        if r.status == 0 { message = (.ok, r.out.trimmingCharacters(in: .whitespacesAndNewlines)); return true }
        message = (.error, r.err.trimmingCharacters(in: .whitespacesAndNewlines)); return false
    }

    func save() async {
        // Validate and save the very same text, whatever is typed while the tool runs.
        let snapshot = text
        guard await validate(snapshot) else { return }
        let backup = Tool.profile.appendingPathExtension("bak")
        do {
            try? FileManager.default.removeItem(at: backup)
            try FileManager.default.copyItem(at: Tool.profile, to: backup)
            try snapshot.write(to: Tool.profile, atomically: true, encoding: .utf8)
            original = snapshot
            message = (.ok, "Saved. The previous version is kept as roll-profile.toml.bak.")
        } catch {
            message = (.error, "Could not save: \(error.localizedDescription)")
        }
    }
}

struct SettingsView: View {
    @StateObject private var m = SettingsModel()
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("roll-profile.toml").font(.headline)
            Text("The one source of truth for cameras, lenses, serials, film stocks, filters and paths — the command line reads the same file. Save is refused unless the tool accepts it.")
                .font(.caption).foregroundStyle(.secondary)
            TextEditor(text: $m.text)
                .font(.system(.body, design: .monospaced))
                .disabled(m.busy)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
            if let msg = m.message { Banner(kind: msg.kind, text: msg.text) }
            HStack {
                Button("Validate") { Task { _ = await m.validate() } }.disabled(m.busy)
                Button("Save") { Task { await m.save() } }.buttonStyle(.borderedProminent).disabled(m.busy || !m.dirty)
                Button("Revert") { m.load() }.disabled(!m.dirty)
                Spacer()
                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([Tool.profile]) }
            }
        }
        .padding(14)
        .onAppear { if !m.dirty { m.load() } }   // never discard unsaved edits on a tab switch
    }
}

// MARK: - Self test
//
// `Filmstamp --selftest R98` drives the real models headlessly against whatever repo the app
// points at (defaults key repoPath) and prints PASS/FAIL per check. It checks the process
// plumbing, the write gate, and the contact sheet and settings paths. It WRITES to the roll.

@MainActor
enum SelfTest {
    static var failures = 0
    static func check(_ ok: Bool, _ what: String) {
        print((ok ? "PASS  " : "FAIL  ") + what)
        if !ok { failures += 1 }
    }

    static func run(_ roll: String) async -> Bool {
        let probs = Tool.problems()
        check(probs.isEmpty, "environment ready \(probs)")

        let m = StampModel()
        m.roll = roll
        check(!m.canWrite, "write disabled before any check")
        await m.refresh()
        check(m.exportPath != nil && m.folderPath != nil, "resolved export and folder: \(m.exportPath ?? "-") | \(m.folderPath ?? "-")")
        check(m.checkOK, "check (dry run) passed: \(m.checkText)")
        check(m.reviewURL != nil && FileManager.default.fileExists(atPath: m.reviewURL?.path ?? ""), "review page produced: \(m.reviewURL?.lastPathComponent ?? "-")")
        check(m.canWrite, "write enabled after a passed check")

        m.roll = "R9"                       // an input changes ...
        m.rollEdited()
        check(!m.canWrite, "write disabled again the moment the input changes")
        m.roll = roll
        m.rollEdited()
        check(!m.canWrite, "write still disabled until re-checked")
        for _ in 0..<40 where !m.canWrite { try? await Task.sleep(nanoseconds: 250_000_000) }
        check(m.canWrite, "write enabled again after the automatic re-check")

        await m.write(force: false)
        check(m.outcome?.kind == .ok, "write verified: \(m.outcome?.text.replacingOccurrences(of: "\n", with: " / ") ?? "-")")
        check(!m.canWrite, "write disabled after writing (no double write)")

        let s = SheetModel()
        s.roll = roll
        s.rollEdited()
        try? await Task.sleep(nanoseconds: 1_500_000_000)
        check(s.folder != nil, "sheet: folder resolved")
        await s.make(replace: false)
        check(s.pdfURL != nil, "sheet made: \(s.message.text.replacingOccurrences(of: "\n", with: " / "))")
        await s.make(replace: false)
        check(s.canReplace && s.message.kind == .error, "sheet: second run is refused and offers Replace")

        let st = SettingsModel()
        st.load()
        check(!st.text.isEmpty, "settings: profile loaded")
        check(await st.validate(), "settings: shipped profile validates")
        st.text = "[time\n"
        check(await !st.validate(), "settings: broken TOML is rejected")
        return failures == 0
    }
}

// MARK: - App

@main
struct FilmstampApp: App {
    init() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--selftest"), i + 1 < args.count {
            let roll = args[i + 1]
            Task { @MainActor in
                let ok = await SelfTest.run(roll)
                print(ok ? "ALL PASSED" : "FAILURES: \(SelfTest.failures)")
                exit(ok ? 0 : 1)
            }
        }
    }

    var body: some Scene {
        WindowGroup("Filmstamp") {
            TabView {
                StampView().tabItem { Label("Stamp", systemImage: "film") }
                SheetView().tabItem { Label("Contact sheet", systemImage: "square.grid.3x3") }
                SettingsView().tabItem { Label("Settings", systemImage: "gearshape") }
            }
            .frame(minWidth: 980, minHeight: 720)
        }
    }
}
