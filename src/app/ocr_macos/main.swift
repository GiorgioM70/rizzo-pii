// rizzo-ocr: OCR di una pagina con il framework Vision di macOS, tutto in locale.
//
// Ingresso: l'immagine della pagina (PNG) sullo stdin.
// Uscita:   JSON su stdout
//   {"width": W, "height": H,
//    "lines": [{"text": "...", "conf": 0.98, "box": [x, y, w, h],
//               "words": [{"text": "...", "box": [x, y, w, h]}]}]}
//   con i riquadri NORMALIZZATI 0..1 e l'origine in ALTO a sinistra (Vision la
//   mette in basso: la conversione e' fatta qui, una volta sola).
// Codici di uscita: 0 ok, 2 immagine non valida, 3 riconoscimento fallito,
// 4 errore nel JSON. Gli errori vanno su stderr.
//
// Compilazione (build_macos.sh):
//   swiftc -O -target arm64-apple-macos11 -o build/ocr/rizzo-ocr src/app/ocr_macos/main.swift
// Lo chiama src/app/ocr.py (MacVisionEngine), un processo per pagina.

import Foundation
import ImageIO
import Vision

struct Word: Codable {
    let text: String
    let box: [Double]
}

struct Line: Codable {
    let text: String
    let conf: Double
    let box: [Double]
    let words: [Word]
}

struct Output: Codable {
    let width: Int
    let height: Int
    let lines: [Line]
}

func topLeft(_ r: CGRect) -> [Double] {
    return [Double(r.minX), Double(1.0 - r.maxY), Double(r.width), Double(r.height)]
}

func die(_ msg: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(code)
}

if CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "--version" {
    print("rizzo-ocr 1")
    exit(0)
}

let input = FileHandle.standardInput.readDataToEndOfFile()
guard !input.isEmpty,
      let source = CGImageSourceCreateWithData(input as CFData, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
else {
    die("immagine non valida", 2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
// italiano prima (atti), inglese come seconda lingua; solo quelle che questa
// versione di macOS sa riconoscere, altrimenti Vision rifiuta la richiesta
var languages = ["it-IT", "en-US"]
if #available(macOS 12.0, *) {
    if let supported = try? request.supportedRecognitionLanguages() {
        languages = languages.filter { supported.contains($0) }
    }
}
if !languages.isEmpty {
    request.recognitionLanguages = languages
}

let handler = VNImageRequestHandler(cgImage: image, options: [:])
do {
    try handler.perform([request])
} catch {
    die("riconoscimento fallito: \(error)", 3)
}

var lines: [Line] = []
for observation in request.results ?? [] {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let s = candidate.string
    let lineBox = topLeft(observation.boundingBox)
    var words: [Word] = []
    var i = s.startIndex
    while i < s.endIndex {
        if s[i].isWhitespace {
            i = s.index(after: i)
            continue
        }
        var j = i
        while j < s.endIndex && !s[j].isWhitespace {
            j = s.index(after: j)
        }
        // riquadro della parola; se Vision non lo da' (o e' degenere) si usa
        // quello della riga intera: meglio coprire troppo che troppo poco
        var box = lineBox
        if let r = try? candidate.boundingBox(for: i..<j) {
            let b = r.boundingBox
            if b.width > 0 && b.height > 0 {
                box = topLeft(b)
            }
        }
        words.append(Word(text: String(s[i..<j]), box: box))
        i = j
    }
    lines.append(Line(text: s, conf: Double(candidate.confidence), box: lineBox, words: words))
}

do {
    let data = try JSONEncoder().encode(Output(width: image.width, height: image.height, lines: lines))
    FileHandle.standardOutput.write(data)
} catch {
    die("json: \(error)", 4)
}
