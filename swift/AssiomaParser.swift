// AssiomaParser.swift
// Reference parser for the Favero Assioma private BLE stream (see PROTOCOL.md).
//
// NOTE: this is a port of assioma_decoder.py, which is validated. It was written
// without access to a Swift compiler, so it has not been compiled.
// Please check it against test_vectors.json before relying on it.
//
// Usage with CoreBluetooth:
//   let char = CBUUID(string: AssiomaParser.privateCharacteristic)
//   peripheral.setNotifyValue(true, for: characteristic)      // read-only, no writes
//   ...
//   func peripheral(_ p: CBPeripheral, didUpdateValueFor c: CBCharacteristic, error: Error?) {
//       guard c.uuid == char, let data = c.value else { return }
//       for msg in parser.feed(data) { handle(msg) }
//   }

import Foundation

public enum RiderPosition: UInt8 {
    case seated = 0, toSeated = 1, standing = 2, toStanding = 3

    /// Value for the FIT `rider_position_type` enum (event 44, rider_position_change).
    public var fitValue: UInt8 {
        switch self {
        case .seated: return 0
        case .standing: return 1
        case .toSeated: return 2
        case .toStanding: return 3
        }
    }
}

/// Power phase angles. Raw units are 360/256 degrees, which is identical to the FIT
/// power_phase units (scale 0.7111111), so the raw bytes can be written to FIT as they are.
public struct PowerPhase: Equatable {
    public let raw: [UInt8]            // start, peakStart, peakEnd, end
    public var startDeg: Double     { Double(raw[0]) * 360.0 / 256.0 }
    public var peakStartDeg: Double { Double(raw[1]) * 360.0 / 256.0 }
    public var peakEndDeg: Double   { Double(raw[2]) * 360.0 / 256.0 }
    public var endDeg: Double       { Double(raw[3]) * 360.0 / 256.0 }
}

public enum AssiomaMessage: Equatable {
    /// 0x10, ~0.1 Hz. balanceRightPct is nil when invalid (0xFF).
    case summary(cadenceRPM: UInt8, powerW: UInt16, energyCounter: UInt16, balanceRightPct: UInt8?)
    /// 0x13, ~1 Hz. Percent values, nil when invalid (0xFF). Raw byte = percent * 2 (= FIT raw).
    case effectiveness(teLeft: Double?, teRight: Double?, psLeft: Double?, psRight: Double?)
    /// 0xE0 (left, ~1 Hz) / 0xE1 (right, sparse). phase is nil when not pedalling (C0 C0 C0 C0).
    case powerPhase(isLeft: Bool, phase: PowerPhase?)
    /// 0xE2, ~2 Hz. PCO in mm, nil when invalid (0x80).
    case pco(leftMM: Int8?, rightMM: Int8?, position: RiderPosition?)
    /// 0x52, ~every 30 s.
    case status(batteryLeftPct: UInt8, batteryRightPct: UInt8)
    case unknown(type: UInt8, payload: [UInt8])
}

public struct AssiomaParser {
    public static let privateCharacteristic = "2701000A-489A-4A48-9DA8-80097E460D77"

    private var buffer: [UInt8] = []
    private var escaping = false

    public init() {}

    /// Feed the raw bytes of one notification and get back every complete message.
    public mutating func feed(_ data: Data) -> [AssiomaMessage] {
        var messages: [AssiomaMessage] = []
        for byte in data {
            if escaping {
                switch byte {
                case 0xDC: buffer.append(0xC0)
                case 0xDD: buffer.append(0xDB)
                default:   buffer.append(byte)
                }
                escaping = false
            } else if byte == 0xDB {
                escaping = true
            } else if byte == 0xC0 {
                if !buffer.isEmpty, let msg = AssiomaParser.parse(frame: buffer) {
                    messages.append(msg)
                }
                buffer.removeAll(keepingCapacity: true)
            } else {
                buffer.append(byte)
            }
        }
        return messages
    }

    /// Parse one unescaped frame: [type][length = count - 2][counter][payload].
    public static func parse(frame f: [UInt8]) -> AssiomaMessage? {
        guard f.count >= 3, Int(f[1]) == f.count - 2 else { return nil }
        let type = f[0]
        let p = Array(f[3...])

        func half(_ v: UInt8) -> Double? { v == 0xFF ? nil : Double(v) / 2.0 }
        func s8(_ v: UInt8) -> Int8? { v == 0x80 ? nil : Int8(bitPattern: v) }

        switch (type, p.count) {
        case (0x10, 6):
            return .summary(cadenceRPM: p[0],
                            powerW: UInt16(p[1]) | UInt16(p[2]) << 8,
                            energyCounter: UInt16(p[3]) | UInt16(p[4]) << 8,
                            balanceRightPct: p[5] == 0xFF ? nil : p[5])
        case (0x13, 4):
            return .effectiveness(teLeft: half(p[0]), teRight: half(p[1]),
                                  psLeft: half(p[2]), psRight: half(p[3]))
        case (0xE0, 6), (0xE1, 6):
            let angles = Array(p[0..<4])
            let invalid = angles == [0xC0, 0xC0, 0xC0, 0xC0]
            return .powerPhase(isLeft: type == 0xE0, phase: invalid ? nil : PowerPhase(raw: angles))
        case (0xE2, 3):
            return .pco(leftMM: s8(p[0]), rightMM: s8(p[1]), position: RiderPosition(rawValue: p[2]))
        case (0x52, 6):
            return .status(batteryLeftPct: p[2], batteryRightPct: p[3])
        default:
            return .unknown(type: type, payload: p)
        }
    }
}

// MARK: - FIT record mapping (global message 20)
//
//   left_right_balance (30)          = 0x80 | rightPct        (0x80 flag = value is the right leg)
//   left/right_torque_effectiveness  (43/44) = raw byte from 0x13 (percent * 2)
//   left/right_pedal_smoothness      (45/46) = raw byte from 0x13 (percent * 2)
//   left_pco / right_pco             (67/68) = Int8 mm from 0xE2
//   left_power_phase       (69) = [raw start, raw end]         from 0xE0
//   left_power_phase_peak  (70) = [raw peakStart, raw peakEnd] from 0xE0
//   right_power_phase      (71) / right_power_phase_peak (72)  from 0xE1
//   rider position: event message, event = 44 (rider_position_change),
//                   data = RiderPosition.fitValue (note: the order differs from Favero's)
