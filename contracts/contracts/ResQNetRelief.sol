// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title ResQNet parametric flood relief
/// @notice Pays a fixed amount to every enrolled household in a zone once
///         (a) the satellite oracle reports the zone flooded for >= hoursThreshold
///         and (b) at least `confirmationsRequired` partner NGOs confirm it.
///         One payout per household per event. No personal data on-chain:
///         households are identified by a salted hash computed off-chain.
contract ResQNetRelief is AccessControl, ReentrancyGuard {
    bytes32 public constant ENROLLER_ROLE = keccak256("ENROLLER_ROLE");
    bytes32 public constant ORACLE_ROLE = keccak256("ORACLE_ROLE");
    bytes32 public constant NGO_ROLE = keccak256("NGO_ROLE");

    struct Household {
        bytes32 zoneId;
        address payable wallet;
        bool active;
        uint64 registeredAt;
    }

    struct FloodEvent {
        uint32 hoursFlooded;
        uint8 confirmations;
        bool triggered;
        uint64 triggeredAt;
        uint256 cursor; // next household index to pay (payouts can span several txs)
    }

    uint256 public payoutAmount;
    uint32 public hoursThreshold;
    uint8 public confirmationsRequired;
    uint256 public maxPayoutsPerTx = 50;
    /// @notice Households must be enrolled at least this long before a payout
    ///         triggers (blocks "enrol after the flood" fraud). 0 for demos, ~7 days in production.
    uint64 public waitingPeriod;

    mapping(bytes32 => Household) public households;          // idHash => household
    mapping(bytes32 => bytes32[]) private zoneHouseholds;      // zoneId => idHashes
    mapping(bytes32 => FloodEvent) public floodEvents;         // key(zone,event) => state
    mapping(bytes32 => mapping(address => bool)) public confirmedBy;
    mapping(bytes32 => mapping(bytes32 => bool)) public paid;  // eventKey => idHash => paid
    mapping(address => uint256) public owed;                   // failed pushes, claimable
    uint256 public totalOwed;

    event HouseholdRegistered(bytes32 indexed idHash, bytes32 indexed zoneId, address wallet);
    event PoolFunded(address indexed from, uint256 amount);
    event FloodReading(bytes32 indexed zoneId, bytes32 indexed eventId, uint32 hoursFlooded);
    event FloodConfirmed(bytes32 indexed zoneId, bytes32 indexed eventId, address indexed ngo, uint8 total);
    event PayoutTriggered(bytes32 indexed zoneId, bytes32 indexed eventId);
    event PayoutSent(bytes32 indexed idHash, bytes32 indexed zoneId, bytes32 indexed eventId, address wallet, uint256 amount);
    event PayoutPaused(bytes32 indexed zoneId, bytes32 indexed eventId, uint256 cursor, string reason);
    event ReliefLogged(bytes32 indexed batchId, string fromParty, string toParty, bytes32 photoHash, address indexed by);

    error AlreadyRegistered();
    error AlreadyConfirmed();
    error ZeroAddress();

    constructor(uint256 _payoutAmount, uint32 _hoursThreshold, uint8 _confirmationsRequired) {
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(ENROLLER_ROLE, msg.sender);
        _grantRole(ORACLE_ROLE, msg.sender);
        payoutAmount = _payoutAmount;
        hoursThreshold = _hoursThreshold;
        confirmationsRequired = _confirmationsRequired;
    }

    // ------------------------------------------------------------------ setup

    function setRule(
        uint256 _payoutAmount,
        uint32 _hoursThreshold,
        uint8 _confirmationsRequired,
        uint256 _maxPerTx,
        uint64 _waitingPeriod
    ) external onlyRole(DEFAULT_ADMIN_ROLE) {
        payoutAmount = _payoutAmount;
        hoursThreshold = _hoursThreshold;
        confirmationsRequired = _confirmationsRequired;
        maxPayoutsPerTx = _maxPerTx;
        waitingPeriod = _waitingPeriod;
    }

    function registerHousehold(bytes32 idHash, bytes32 zoneId, address payable wallet)
        external
        onlyRole(ENROLLER_ROLE)
    {
        if (wallet == address(0)) revert ZeroAddress();
        if (households[idHash].active) revert AlreadyRegistered();
        households[idHash] = Household(zoneId, wallet, true, uint64(block.timestamp));
        zoneHouseholds[zoneId].push(idHash);
        emit HouseholdRegistered(idHash, zoneId, wallet);
    }

    function fundPool() public payable {
        emit PoolFunded(msg.sender, msg.value);
    }

    receive() external payable {
        fundPool();
    }

    // --------------------------------------------------------------- triggers

    function eventKey(bytes32 zoneId, bytes32 eventId) public pure returns (bytes32) {
        return keccak256(abi.encodePacked(zoneId, eventId));
    }

    function submitFloodReading(bytes32 zoneId, bytes32 eventId, uint32 hoursFlooded)
        external
        onlyRole(ORACLE_ROLE)
        nonReentrant
    {
        bytes32 k = eventKey(zoneId, eventId);
        floodEvents[k].hoursFlooded = hoursFlooded;
        emit FloodReading(zoneId, eventId, hoursFlooded);
        _tryPayout(zoneId, eventId, k);
    }

    function confirmFlood(bytes32 zoneId, bytes32 eventId) external onlyRole(NGO_ROLE) nonReentrant {
        bytes32 k = eventKey(zoneId, eventId);
        if (confirmedBy[k][msg.sender]) revert AlreadyConfirmed();
        confirmedBy[k][msg.sender] = true;
        floodEvents[k].confirmations += 1;
        emit FloodConfirmed(zoneId, eventId, msg.sender, floodEvents[k].confirmations);
        _tryPayout(zoneId, eventId, k);
    }

    /// @notice Continue a payout that was paused (pool empty or batch limit).
    function processPayouts(bytes32 zoneId, bytes32 eventId) external nonReentrant {
        bytes32 k = eventKey(zoneId, eventId);
        if (floodEvents[k].triggered) _pay(zoneId, eventId, k);
    }

    function _tryPayout(bytes32 zoneId, bytes32 eventId, bytes32 k) internal {
        FloodEvent storage e = floodEvents[k];
        if (e.triggered) return;
        if (e.hoursFlooded < hoursThreshold || e.confirmations < confirmationsRequired) return;
        e.triggered = true;
        e.triggeredAt = uint64(block.timestamp);
        emit PayoutTriggered(zoneId, eventId);
        _pay(zoneId, eventId, k);
    }

    function _pay(bytes32 zoneId, bytes32 eventId, bytes32 k) internal {
        FloodEvent storage e = floodEvents[k];
        bytes32[] storage list = zoneHouseholds[zoneId];
        uint256 sent = 0;
        while (e.cursor < list.length) {
            if (sent >= maxPayoutsPerTx) {
                emit PayoutPaused(zoneId, eventId, e.cursor, "batch limit");
                return;
            }
            bytes32 idHash = list[e.cursor];
            Household storage h = households[idHash];
            if (h.active && !paid[k][idHash] && h.registeredAt + waitingPeriod < e.triggeredAt + 1) {
                if (address(this).balance - totalOwed < payoutAmount) {
                    emit PayoutPaused(zoneId, eventId, e.cursor, "pool empty");
                    return;
                }
                paid[k][idHash] = true;
                (bool ok,) = h.wallet.call{value: payoutAmount, gas: 10000}("");
                if (!ok) {
                    owed[h.wallet] += payoutAmount;
                    totalOwed += payoutAmount;
                }
                emit PayoutSent(idHash, zoneId, eventId, h.wallet, payoutAmount);
                sent++;
            }
            e.cursor++;
        }
    }

    function withdrawOwed() external nonReentrant {
        uint256 amt = owed[msg.sender];
        owed[msg.sender] = 0;
        totalOwed -= amt;
        (bool ok,) = payable(msg.sender).call{value: amt}("");
        require(ok, "withdraw failed");
    }

    // ------------------------------------------------------------- relief log

    function logRelief(bytes32 batchId, string calldata fromParty, string calldata toParty, bytes32 photoHash)
        external
        onlyRole(NGO_ROLE)
    {
        emit ReliefLogged(batchId, fromParty, toParty, photoHash, msg.sender);
    }

    // ------------------------------------------------------------------ views

    function zoneHouseholdCount(bytes32 zoneId) external view returns (uint256) {
        return zoneHouseholds[zoneId].length;
    }

    function poolBalance() external view returns (uint256) {
        return address(this).balance - totalOwed;
    }
}
