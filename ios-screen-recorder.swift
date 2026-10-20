// Records the screen of a USB-connected iPhone (same mechanism QuickTime uses)
// so it can be lined up with Proxyman traffic.
//
// Output (in <output-dir>/session-<timestamp>/):
//   screen.mp4          H.264 video of the whole session
//   frames/<epochMs>.jpg still snapshots every --interval ms (default 500),
//                        named by wall-clock capture time
//   session.json        wall-clock anchor: video t=0 == startEpochMs
//
// Build:  swiftc -O ios-screen-recorder.swift -o ios-screen-recorder
// Run:    ./ios-screen-recorder [output-dir] [--interval ms] [--no-frames]   (Ctrl+C to stop)

import AVFoundation
import CoreImage
import CoreMediaIO
import Foundation

setvbuf(stdout, nil, _IONBF, 0)

// MARK: - Args

var outRoot = "."
var frameIntervalMs: Int64 = 500
var saveFrames = true
var argv = Array(CommandLine.arguments.dropFirst())
while !argv.isEmpty {
    let a = argv.removeFirst()
    switch a {
    case "--interval": frameIntervalMs = Int64(argv.removeFirst()) ?? 500
    case "--no-frames": saveFrames = false
    default: outRoot = a
    }
}

// MARK: - Device

// Expose iOS devices as AVCaptureDevices (off by default).
var prop = CMIOObjectPropertyAddress(
    mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
    mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
    mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
var allow: UInt32 = 1
CMIOObjectSetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &prop, 0, nil,
                          UInt32(MemoryLayout<UInt32>.size), &allow)

func findPhone() -> AVCaptureDevice? {
    AVCaptureDevice.DiscoverySession(deviceTypes: [.external], mediaType: .muxed, position: .unspecified)
        .devices.first
}

var found: AVCaptureDevice?
for _ in 0..<30 {
    found = findPhone()
    if found != nil { break }
    RunLoop.current.run(until: Date().addingTimeInterval(1))
}
guard let phone = found else {
    print("No iPhone capture device found. Unlock the phone, trust this Mac, and close QuickTime.")
    exit(1)
}
let deviceName = phone.localizedName
print("Device: \(deviceName)")

// MARK: - Output paths

let stamp: String = {
    let f = DateFormatter()
    f.dateFormat = "yyyyMMdd-HHmmss"
    return f.string(from: Date())
}()
let sessionDir = URL(fileURLWithPath: outRoot).appendingPathComponent("session-\(stamp)")
let framesDir = sessionDir.appendingPathComponent("frames")
try FileManager.default.createDirectory(at: framesDir, withIntermediateDirectories: true)
let movieURL = sessionDir.appendingPathComponent("screen.mp4")
let metaURL = sessionDir.appendingPathComponent("session.json")

func nowMs() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }

// MARK: - Recorder

final class Recorder: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    var writer: AVAssetWriter?
    var input: AVAssetWriterInput?
    var firstPTS: CMTime?
    var startEpochMs: Int64 = 0
    var lastFrameMs: Int64 = 0
    var frameCount = 0
    var stillCount = 0
    var stopping = false
    let ci = CIContext()

    func captureOutput(_ output: AVCaptureOutput, didOutput sb: CMSampleBuffer, from c: AVCaptureConnection) {
        guard !stopping, let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sb)
        let wall = nowMs()

        if writer == nil {
            let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
            let wr = try! AVAssetWriter(outputURL: movieURL, fileType: .mp4)
            let inp = AVAssetWriterInput(mediaType: .video, outputSettings: [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: w, AVVideoHeightKey: h,
                AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 4_000_000],
            ])
            inp.expectsMediaDataInRealTime = true
            wr.add(inp)
            wr.startWriting()
            wr.startSession(atSourceTime: pts)
            writer = wr; input = inp; firstPTS = pts; startEpochMs = wall
            writeMeta(width: w, height: h)
            print("Recording \(w)x\(h) -> \(sessionDir.path)\nPress Ctrl+C to stop.")
        }

        if let inp = input, inp.isReadyForMoreMediaData { inp.append(sb) }
        frameCount += 1

        if saveFrames && wall - lastFrameMs >= frameIntervalMs {
            lastFrameMs = wall
            let img = CIImage(cvPixelBuffer: pb)
            let url = framesDir.appendingPathComponent("\(wall).jpg")
            try? ci.writeJPEGRepresentation(of: img, to: url,
                                            colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                                            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.6])
            stillCount += 1
        }
    }

    func writeMeta(width: Int, height: Int, endEpochMs: Int64? = nil) {
        var meta: [String: Any] = [
            "video": "screen.mp4",
            "framesDir": "frames",
            "startEpochMs": startEpochMs,
            "startISO": ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: Double(startEpochMs) / 1000)),
            "device": deviceName,
            "width": width, "height": height,
            "frameIntervalMs": frameIntervalMs,
        ]
        if let e = endEpochMs { meta["endEpochMs"] = e; meta["frames"] = frameCount; meta["stills"] = stillCount }
        try? JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys]).write(to: metaURL)
    }

    func stop(_ done: @escaping () -> Void) {
        // Ctrl+C reaches us twice (terminal SIGINT + capture.sh's forwarded kill) — finish only once.
        guard !stopping else { return }
        stopping = true
        guard let w = writer, let inp = input else {
            print("No frames were received. Make sure the phone is unlocked and the screen is on.")
            try? FileManager.default.removeItem(at: sessionDir)
            done(); return
        }
        inp.markAsFinished()
        w.finishWriting {
            let dims = inp.outputSettings ?? [:]
            self.writeMeta(width: dims[AVVideoWidthKey] as? Int ?? 0,
                           height: dims[AVVideoHeightKey] as? Int ?? 0,
                           endEpochMs: nowMs())
            print("Saved \(self.frameCount) frames, \(self.stillCount) stills -> \(sessionDir.path)")
            if w.status == .failed { print("Writer error: \(String(describing: w.error))") }
            done()
        }
    }
}

let recorder = Recorder()
let session = AVCaptureSession()
do {
    let input = try AVCaptureDeviceInput(device: phone)
    session.addInput(input)
} catch {
    print("Cannot open device: \(error)")
    exit(1)
}
let videoOut = AVCaptureVideoDataOutput()
videoOut.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
videoOut.alwaysDiscardsLateVideoFrames = true
let queue = DispatchQueue(label: "capture")
videoOut.setSampleBufferDelegate(recorder, queue: queue)
session.addOutput(videoOut)
session.startRunning()

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
var sources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    let s = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    s.setEventHandler {
        print("\nStopping...")
        queue.async { recorder.stop { session.stopRunning(); exit(0) } }
    }
    s.resume()
    sources.append(s)
}

RunLoop.main.run()
