// Recognize on-screen text in screenshots (macOS Vision) so screens can be named.
// Build:  swiftc -O screen-ocr.swift -o screen-ocr
// Usage:  ./screen-ocr image1.jpg image2.jpg ...   -> JSON array on stdout
//   [{"path": "...", "lines": [{"text": "...", "conf": 0.98, "x": 0.1, "y": 0.2, "w": 0.5, "h": 0.03}]}]
//   x/y/w/h are normalized with the origin at the TOP-left.

import Foundation
import Vision
import AppKit

var results: [[String: Any]] = []
for path in CommandLine.arguments.dropFirst() {
    var lines: [[String: Any]] = []
    if let img = NSImage(contentsOfFile: path),
       let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = true
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
        for obs in req.results ?? [] {
            guard let top = obs.topCandidates(1).first else { continue }
            let b = obs.boundingBox
            lines.append(["text": top.string, "conf": top.confidence,
                          "x": b.minX, "y": 1 - b.maxY, "w": b.width, "h": b.height])
        }
    }
    results.append(["path": path, "lines": lines])
}
let data = try! JSONSerialization.data(withJSONObject: results, options: [])
FileHandle.standardOutput.write(data)
