const { expect } = require("chai");
const { ethers } = require("hardhat");

const b32 = (s) => ethers.encodeBytes32String(s);
const PAYOUT = ethers.parseEther("0.001");

describe("ResQNetRelief", function () {
  let c, admin, ngo1, ngo2, ngo3, h1, h2, h3, stranger;
  const ZONE = b32("HYD-MLK");
  const OTHER = b32("HYD-UPL");
  const EVENT = b32("HYD-2020-10");

  beforeEach(async () => {
    [admin, ngo1, ngo2, ngo3, h1, h2, h3, stranger] = await ethers.getSigners();
    const F = await ethers.getContractFactory("ResQNetRelief");
    c = await F.deploy(PAYOUT, 48, 2);
    const NGO = await c.NGO_ROLE();
    for (const n of [ngo1, ngo2, ngo3]) await c.grantRole(NGO, n.address);
    await c.registerHousehold(ethers.id("household-1"), ZONE, h1.address);
    await c.registerHousehold(ethers.id("household-2"), ZONE, h2.address);
    await c.registerHousehold(ethers.id("household-3"), OTHER, h3.address);
    await c.fundPool({ value: ethers.parseEther("1") });
  });

  it("stores only hash + zone, rejects duplicates", async () => {
    const h = await c.households(ethers.id("household-1"));
    expect(h.zoneId).to.equal(ZONE);
    await expect(c.registerHousehold(ethers.id("household-1"), ZONE, h1.address))
      .to.be.revertedWithCustomError(c, "AlreadyRegistered");
  });

  it("only roles can call oracle / NGO / enrol functions", async () => {
    await expect(c.connect(stranger).submitFloodReading(ZONE, EVENT, 72)).to.be.reverted;
    await expect(c.connect(stranger).confirmFlood(ZONE, EVENT)).to.be.reverted;
    await expect(c.connect(stranger).registerHousehold(ethers.id("x"), ZONE, stranger.address)).to.be.reverted;
  });

  it("does not pay on satellite reading alone or NGO confirmations alone", async () => {
    await c.submitFloodReading(ZONE, EVENT, 72);
    await c.connect(ngo1).confirmFlood(ZONE, EVENT);
    expect((await c.floodEvents(await c.eventKey(ZONE, EVENT))).triggered).to.equal(false);
  });

  it("does not pay when flooded less than threshold", async () => {
    await c.submitFloodReading(ZONE, EVENT, 24);
    await c.connect(ngo1).confirmFlood(ZONE, EVENT);
    await c.connect(ngo2).confirmFlood(ZONE, EVENT);
    expect((await c.floodEvents(await c.eventKey(ZONE, EVENT))).triggered).to.equal(false);
  });

  it("pays every household in the zone once both conditions hold", async () => {
    await c.submitFloodReading(ZONE, EVENT, 72);
    await c.connect(ngo1).confirmFlood(ZONE, EVENT);
    const before1 = await ethers.provider.getBalance(h1.address);
    const before3 = await ethers.provider.getBalance(h3.address);
    await expect(c.connect(ngo2).confirmFlood(ZONE, EVENT))
      .to.emit(c, "PayoutTriggered")
      .and.to.emit(c, "PayoutSent");
    expect(await ethers.provider.getBalance(h1.address)).to.equal(before1 + PAYOUT);
    expect(await ethers.provider.getBalance(h3.address)).to.equal(before3); // other zone untouched
  });

  it("order does not matter: confirmations first, reading last", async () => {
    await c.connect(ngo1).confirmFlood(ZONE, EVENT);
    await c.connect(ngo2).confirmFlood(ZONE, EVENT);
    await expect(c.submitFloodReading(ZONE, EVENT, 50)).to.emit(c, "PayoutTriggered");
  });

  it("one payout per household per event, and one confirmation per NGO", async () => {
    await c.submitFloodReading(ZONE, EVENT, 72);
    await c.connect(ngo1).confirmFlood(ZONE, EVENT);
    await c.connect(ngo2).confirmFlood(ZONE, EVENT);
    const bal = await ethers.provider.getBalance(h1.address);
    await c.connect(ngo3).confirmFlood(ZONE, EVENT);
    await c.submitFloodReading(ZONE, EVENT, 96);
    await c.processPayouts(ZONE, EVENT);
    expect(await ethers.provider.getBalance(h1.address)).to.equal(bal);
    await expect(c.connect(ngo1).confirmFlood(ZONE, EVENT)).to.be.revertedWithCustomError(c, "AlreadyConfirmed");
  });

  it("pauses when pool is empty and resumes after funding", async () => {
    const F = await ethers.getContractFactory("ResQNetRelief");
    const d = await F.deploy(PAYOUT, 48, 1);
    await d.grantRole(await d.NGO_ROLE(), ngo1.address);
    await d.registerHousehold(ethers.id("a"), ZONE, h1.address);
    await d.registerHousehold(ethers.id("b"), ZONE, h2.address);
    await d.fundPool({ value: PAYOUT }); // enough for one
    await d.submitFloodReading(ZONE, EVENT, 72);
    await expect(d.connect(ngo1).confirmFlood(ZONE, EVENT)).to.emit(d, "PayoutPaused");
    const bal2 = await ethers.provider.getBalance(h2.address);
    await d.fundPool({ value: PAYOUT });
    await d.processPayouts(ZONE, EVENT);
    expect(await ethers.provider.getBalance(h2.address)).to.equal(bal2 + PAYOUT);
  });

  it("does not pay households enrolled after the trigger", async () => {
    await c.submitFloodReading(ZONE, EVENT, 72);
    await c.connect(ngo1).confirmFlood(ZONE, EVENT);
    await c.connect(ngo2).confirmFlood(ZONE, EVENT);
    await ethers.provider.send("evm_increaseTime", [3600]);
    await c.registerHousehold(ethers.id("late"), ZONE, stranger.address);
    const bal = await ethers.provider.getBalance(stranger.address);
    await c.connect(admin).processPayouts(ZONE, EVENT);
    expect(await ethers.provider.getBalance(stranger.address)).to.equal(bal);
  });

  it("logs relief handovers from NGOs", async () => {
    await expect(c.connect(ngo1).logRelief(b32("BATCH-1"), "Warehouse A", "Camp 3", ethers.id("photo")))
      .to.emit(c, "ReliefLogged");
  });
});
