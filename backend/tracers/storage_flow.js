({
  logs: [],
  faults: [],
  previousResultIndex: -1,
  pendingCall: null,
  codeAddressByDepth: {},
  lastDepth: 0,
  opcodeCount: 0,

  // Geth's Javascript tracer API exposes addresses as byte arrays.
  byteHex: function (value) {
    var text = value.toString(16);
    return text.length < 2 ? "0" + text : text;
  },

  arrayHex: function (values) {
    var output = "0x";
    for (var index = 0; index < values.length; index++) {
      output += this.byteHex(values[index]);
    }
    return output;
  },

  storageAddress: function (log) {
    return this.arrayHex(log.contract.getAddress());
  },

  stackHex: function (stack) {
    var output = [];
    // Keep the same bottom-to-top order as Geth's struct logger. The top item
    // is therefore output[output.length - 1] in Python.
    for (var index = stack.length() - 1; index >= 0; index--) {
      output.push("0x" + stack.peek(index).toString(16));
    }
    return output;
  },

  isCall: function (opcode) {
    return opcode === "CALL" || opcode === "CALLCODE" ||
      opcode === "DELEGATECALL" || opcode === "STATICCALL";
  },

  isHash: function (opcode) {
    return opcode === "SHA3" || opcode === "KECCAK256" || opcode === "KECCAK";
  },

  isInteresting: function (opcode) {
    return this.isCall(opcode) || this.isHash(opcode) ||
      opcode === "CREATE" || opcode === "CREATE2" ||
      opcode === "SLOAD" || opcode === "SSTORE" ||
      opcode === "STOP" || opcode === "RETURN" || opcode === "REVERT" ||
      opcode === "INVALID" || opcode === "SELFDESTRUCT";
  },

  step: function (log) {
    this.opcodeCount++;
    var opcode = log.op.toString();
    var depth = log.getDepth();
    var storageAddress = this.storageAddress(log);
    var enteredFromCall = false;

    // The step immediately after CALL tells us whether a child frame was
    // actually entered. Annotate the CALL instead of returning that extra
    // opcode solely for Python to inspect it.
    if (this.pendingCall !== null) {
      var entered = depth > this.pendingCall.depth;
      var callRecord = this.logs[this.pendingCall.index];
      callRecord.enteredChildCall = entered;
      callRecord.nextPC = log.getPC();
      callRecord.nextGas = log.getGas();
      if (entered) {
        this.codeAddressByDepth[depth] = this.pendingCall.target;
        enteredFromCall = true;
      }
      this.pendingCall = null;
    }

    if (!this.codeAddressByDepth.hasOwnProperty(depth) ||
        (depth > this.lastDepth && !enteredFromCall)) {
      // This covers the root frame and CREATE frames, whose address is not on
      // the CREATE stack before execution begins.
      this.codeAddressByDepth[depth] = storageAddress;
    }
    this.lastDepth = depth;

    // SLOAD and KECCAK results appear on the stack at the following EVM step.
    // Store the result directly on the original record, then discard this step
    // if it is otherwise irrelevant.
    if (this.previousResultIndex >= 0) {
      if (log.stack.length() > 0) {
        this.logs[this.previousResultIndex].result =
          "0x" + log.stack.peek(0).toString(16);
      }
      this.previousResultIndex = -1;
    }

    if (!this.isInteresting(opcode)) {
      return;
    }

    var stack = this.stackHex(log.stack);
    var memory = "0x";
    var memoryOffset = null;
    var memorySize = null;

    // Balance and allowance mapping hashes consume exactly 64 bytes. Return
    // only that slice, not the entire EVM memory accumulated so far.
    if (this.isHash(opcode) && log.stack.length() >= 2) {
      memoryOffset = parseInt(log.stack.peek(0).toString(10), 10);
      memorySize = parseInt(log.stack.peek(1).toString(10), 10);
      if (memorySize === 64 && memoryOffset >= 0 &&
          isFinite(memoryOffset) && memoryOffset + memorySize <= log.memory.length()) {
        memory = toHex(log.memory.slice(memoryOffset, memoryOffset + memorySize));
      }
    }

    var storage = {};
    if (opcode === "SSTORE" && stack.length >= 2) {
      storage[stack[stack.length - 1]] = stack[stack.length - 2];
    }

    var record = {
      pc: log.getPC(),
      op: opcode,
      gas: log.getGas(),
      gasCost: log.getCost(),
      depth: depth,
      address: this.codeAddressByDepth[depth],
      storageAddress: storageAddress,
      stack: stack,
      memory: memory,
      memoryOffset: memoryOffset,
      memorySize: memorySize,
      storage: storage
    };
    this.logs.push(record);
    var recordIndex = this.logs.length - 1;

    if (opcode === "SLOAD" || this.isHash(opcode)) {
      this.previousResultIndex = recordIndex;
    }

    if (this.isCall(opcode) && log.stack.length() >= 2) {
      var target = "0x" + log.stack.peek(1).toString(16);
      target = "0x" + target.slice(2).slice(-40);
      this.pendingCall = {index: recordIndex, depth: depth, target: target};
      record.enteredChildCall = false;
      record.nextPC = null;
      record.nextGas = null;
    }
  },

  fault: function (log) {
    this.faults.push([this.logs.length, log.getPC(), String(log.getError())]);
  },

  result: function () {
    return {
      structLogs: this.logs,
      faults: this.faults,
      filtered: true,
      opcodeCount: this.opcodeCount
    };
  }
})
