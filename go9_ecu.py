"""go9_ecu.py - OBD-II ECU emulator to activate the GO9 on the bench.

*** STUB - Phase 1, BLOCKED on the Phase-0 capture ***
(see docs/go9_integration_proposal.md and docs/captures/go9_candump_*.log)

Do not implement blind: the responder must answer exactly the PIDs the GO9 polls,
and only handle mode-09 VIN if the capture shows the GO9 gates on it.

Target design (fill in after the capture):
  - Listen on can0 for requests to 0x7DF (functional) / 0x7E0 (physical).
  - Respond from 0x7E8:
      * mode 01 PID 0C (RPM)   -> nonzero, so the GO9 sees engine running / trip
      * mode 01 PID 0D (speed), 05 (coolant), + whatever else it insists on
      * mode 01 PID 00/20/...  supported-PID bitmasks it probes
      * mode 09 PID 02 (VIN)   -> ISO-TP multiframe w/ flow control (if required)

Open questions gating this file - see docs/go9_integration_proposal.md:
  Q1 protocol/baud (500k vs 250k)   Q2 VIN gate?   Q5 bench termination

Reference: OBD-II CAN IDs 0x7DF request / 0x7E0-7 physical / 0x7E8-F response.
"""
from __future__ import annotations


class GO9EcuSim:
    """Placeholder. Implement respond() against the Phase-0 capture."""

    def __init__(self, channel: str = "can0", rpm: int = 800):
        self.channel = channel
        self.rpm = rpm

    def run(self) -> None:
        raise NotImplementedError(
            "ECU emulator is Phase 1 - capture the GO9's polls first "
            "(./scripts/sniff.sh), then target respond() to exactly those PIDs."
        )


if __name__ == "__main__":
    print(
        "go9_ecu is a Phase-1 stub. Run ./scripts/sniff.sh to capture the GO9's "
        "OBD polls first, then this gets targeted to exactly those PIDs."
    )
