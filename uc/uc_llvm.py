from __future__ import annotations
import argparse
import sys
from ctypes import CFUNCTYPE, c_int
from pathlib import Path
from typing import Literal, TextIO
from graphviz import Source
from llvmlite import binding, ir
from llvmlite.binding import ExecutionEngine, ModuleRef, value
from llvmlite.ir import Constant, Function, Module
from uc.uc_ast import FuncDef, Program
from uc.uc_block import BasicBlock, Block, BlockVisitor, FunctionBlock, GlobalBlock
from uc.uc_code import CodeGenerator
from uc.uc_ir import GlobalInstr, Instruction, Variable
from uc.uc_parser import UCParser
from uc.uc_sema import NodeVisitor, Visitor
from uc.uc_type import BoolType, CharType, FloatType, IntType, StringType


def make_bytearray(buf: str, encoding: Literal["ascii", "utf8"] = "uft8") -> Constant:
    # Make a byte array constant from *buf*.
    data = bytearray(buf + "\0", encoding=encoding)
    lltype = ir.ArrayType(ir.IntType(8), len(data))
    return Constant(lltype, data)


def make_constant(value: int | bool | str | float | list[int | bool | str | float]) -> ir.Constant:
    if isinstance(value, bool):
        return ir.Constant(BoolType.as_llvm(), value)
    elif isinstance(value, int):
        return ir.Constant(IntType.as_llvm(), value)
    elif isinstance(value, float):
        return ir.Constant(FloatType.as_llvm(), value)
    elif isinstance(value, str):
        return ir.Constant(CharType.as_llvm(), ord(value))
    else:
        elements = [make_constant(item) for item in value]
        return ir.Constant.literal_array(elements)


class LLVMFunctionVisitor(BlockVisitor[Function]):
    def __init__(self, module: Module) -> None:
        super().__init__(lambda _: None)
        self.module = module
        self.loc = {}

    def _get_loc(self, target):
        try:
            if target[0] == "%":
                return self.loc[target]
            elif target[0] == "@":
                return self.module.get_global(target[1:])
        except KeyError:
            return None

    def _global_constant(
        self, builder_or_module, name: str, value: Constant, linkage: str = "internal"
    ) -> ir.GlobalVariable:
        # Get or create a (LLVM module-)global constant with *name* or *value*.
        if isinstance(builder_or_module, Module):
            mod = builder_or_module
        else:
            mod = builder_or_module.module
        data = ir.GlobalVariable(mod, value.type, name=name)
        data.linkage = linkage
        data.global_constant = True
        data.initializer = value
        data.align = 1
        return data

    def _cio(self, fname: str, format: str, *target):
        # Make global constant for string format
        mod = self.builder.module
        fmt_bytes = make_bytearray(format)
        global_fmt = self._global_constant(mod, mod.get_unique_name(".fmt"), fmt_bytes)
        fn = mod.get_global(fname)
        ptr_fmt = self.builder.bitcast(global_fmt, ir.IntType(8).as_pointer())
        return self.builder.call(fn, [ptr_fmt] + list(target))

    def _build_print(self, val_type, target):
        if target:
            # get the object assigned to target
            value = self._get_loc(target)
            if val_type == "int":
                self._cio("printf", "%d", value)
            elif val_type == "float":
                self._cio("printf", "%.2f", value)
            elif val_type == "char":
                self._cio("printf", "%c", value)
            elif val_type == "string":
                self._cio("printf", "%s", value)
        else:
            self._cio("printf", "\n")

    def build(self, inst: Instruction) -> None:
        opcode, ctype, modifier = self._extract_operation(inst[0])
        if hasattr(self, "_build_" + opcode):
            args = inst[1:] if len(inst) > 1 else (None,)
            if not modifier:
                getattr(self, "_build_" + opcode)(ctype, *args)
            else:
                getattr(self, "_build_" + opcode + "_")(ctype, *inst[1:], **modifier)
        else:
            print("Warning: No _build_" + opcode + "() method", flush=True)

    def visit_FunctionBlock(self, block: FunctionBlock, _: None) -> Function:
        return Function(self.module, block.fntype.as_llvm(), block.name)

    def visit_BasicBlock(self, block: BasicBlock, func: Function) -> None:
        # TODO: Complete
        # Create the LLVM function when visiting its first block
        # First visit of the block should create its LLVM equivalent
        # Second visit should create the LLVM instructions within the block
        pass

    def visit_ConditionBlock(self, block):
        # TODO: Complete
        # Create the LLVM function when visiting its first block
        # First visit of the block should create its LLVM equivalent
        # Second visit should create the LLVM instructions within the block
        pass


class LLVMModuleVisitor(BlockVisitor[Module]):
    def __init__(self):
        super().__init__(self.build_module)

    def build_module(self, program: Block) -> Module:
        assert isinstance(program, GlobalBlock)
        module = ir.Module(program.name)
        module.triple = binding.get_default_triple()
        return module

    def declare_printf(self, mod: Module) -> ir.types.Function:
        voidptr_ty = ir.IntType(8).as_pointer()
        printf_ty = ir.FunctionType(ir.IntType(32), [voidptr_ty], var_arg=True)
        return ir.Function(mod, printf_ty, name="printf")

    def declare_scanf(self, mod: Module) -> ir.types.Function:
        voidptr_ty = ir.IntType(8).as_pointer()
        scanf_ty = ir.FunctionType(ir.IntType(32), [voidptr_ty], var_arg=True)
        return ir.Function(mod, scanf_ty, name="scanf")

    def declare_global(
        self, mod: Module, instr: GlobalInstr, *, constant: bool = False
    ) -> ir.GlobalVariable:
        var = ir.GlobalVariable(mod, instr.type.as_llvm(), instr.varname.format())
        var.global_constant = constant
        if instr.value is None:
            pass
        elif isinstance(instr.value, Variable):
            var.initializer = mod.get_global(instr.value.format())
        elif isinstance(instr.type, StringType):
            var.initializer = make_bytearray(instr.value)
        else:
            var.initializer = make_constant(instr.value)
        return var

    def visit_GlobalBlock(self, program: GlobalBlock, module: Module) -> None:
        # declare external functions
        self.declare_printf(module)
        self.declare_scanf(module)
        # and global variables
        for instr in program.cdata:
            self.declare_global(module, instr, constant=True)
        for instr in program.data:
            self.declare_global(module, instr)


class LLVMCodeGenerator(NodeVisitor[None]):
    def __init__(self, viewcfg: bool) -> None:
        self.viewcfg = viewcfg
        binding.initialize()
        binding.initialize_native_target()
        binding.initialize_native_asmprinter()

        self.engine = self._create_execution_engine()

    def _create_execution_engine(self) -> ExecutionEngine:
        """
        Create an ExecutionEngine suitable for JIT code generation on
        the host CPU.  The engine is reusable for an arbitrary number of
        modules.
        """
        target = binding.Target.from_default_triple()
        target_machine = target.create_target_machine()
        # And an execution engine with an empty backing module
        backing_mod = binding.parse_assembly("")
        return binding.create_mcjit_compiler(backing_mod, target_machine)

    def _compile_ir(self) -> ModuleRef:
        """
        Compile the LLVM IR string with the given engine.
        The compiled module object is returned.
        """
        # Create a LLVM module object from the IR
        llvm_ir = str(self.module)
        mod = binding.parse_assembly(llvm_ir)
        mod.verify()
        # Now add the module and make sure it is ready for execution
        self.engine.add_module(mod)
        self.engine.finalize_object()
        self.engine.run_static_constructors()
        return mod

    def save_ir(self, output_file: TextIO) -> None:
        output_file.write(str(self.module))

    def execute_ir(self, opt: Literal["ctm", "dce", "cfg", "all", None], opt_file: TextIO) -> int:
        mod = self._compile_ir()

        if opt:
            # apply some optimization passes on module
            pmb = binding.create_pass_manager_builder()
            pm = binding.create_module_pass_manager()

            pmb.opt_level = 0
            if opt == "ctm" or opt == "all":
                # Sparse conditional constant propagation and merging
                pm.add_sccp_pass()
                # Merges duplicate global constants together
                pm.add_constant_merge_pass()
                # Combine inst to form fewer, simple inst
                # This pass also does algebraic simplification
                pm.add_instruction_combining_pass()
            if opt == "dce" or opt == "all":
                pm.add_dead_code_elimination_pass()
            if opt == "cfg" or opt == "all":
                # Performs dead code elimination and basic block merging
                pm.add_cfg_simplification_pass()

            pmb.populate(pm)
            pm.run(mod)
            opt_file.write(str(mod))

        # Obtain a pointer to the compiled 'main' - it's the address of its JITed code in memory.
        main_ptr = self.engine.get_function_address("main")
        # To convert an address to an actual callable thing we have to use
        # CFUNCTYPE, and specify the arguments & return type.
        main_function = CFUNCTYPE(c_int)(main_ptr)
        # Now 'main_function' is an actual callable we can invoke
        return main_function()

    def visit_Program(self, node: Program) -> None:
        bb = LLVMModuleVisitor()
        self.module = bb.visit(node.cfg)
        # # Visit all the function definitions and emit the llvm code from the
        # # uCIR code stored inside basic blocks.
        # for decl in node.gdecls:
        #     self.visit(decl)

    def visit_FuncDef(self, node: FuncDef) -> None:
        # _decl.cfg contains the Control Flow Graph for the function
        bb = LLVMFunctionVisitor(self.module)
        # Visit the CFG to define the Function and Create the Basic Blocks
        bb.visit(node.cfg)
        # Visit CFG again to create the instructions inside Basic Blocks
        function = bb.visit(node.cfg)

        if self.viewcfg:
            dot = binding.get_function_cfg(function)
            gv: Source = binding.view_dot_graph(dot, f"{node.funcname}.ll.gv", False)
            gv.view(quiet=True, quiet_view=True)


if __name__ == "__main__":

    # create argument parser
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "input_file",
        help="Path to file to be used to generate LLVM IR. By default, this script runs the LLVM IR without any optimizations.",
        type=Path,
    )
    parser.add_argument(
        "-c",
        "--cfg",
        help="show the CFG of the optimized uCIR for each function in pdf format",
        action="store_true",
    )
    parser.add_argument(
        "--llvm-opt",
        default=None,
        choices=["ctm", "dce", "cfg", "all"],
        help="specify which llvm pass optimizations should be enabled",
    )
    args = parser.parse_args()

    create_cfg = args.cfg
    llvm_opt = args.llvm_opt

    # get input path
    input_file: Path = args.input_file

    # check if file exists
    if not input_file.exists():
        print("Input", input_file, "not found", file=sys.stderr)
        sys.exit(1)

    # set error function
    p = UCParser()
    # open file and parse it
    with open(input_file) as file:
        ast = p.parse(file)

    sema = Visitor()
    sema.visit(ast)

    gen = CodeGenerator(False)
    gen.visit(ast)

    llvm = LLVMCodeGenerator(create_cfg)
    llvm.visit(ast)
    # llvm.execute_ir(llvm_opt, None)
    with open("test.ir", "w") as irfile:
        llvm.save_ir(irfile)
