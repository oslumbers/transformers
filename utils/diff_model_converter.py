import importlib
import re

import libcst as cst
from libcst import matchers as m


# Should we use the scope to figure out if classes are imported and inherited from
# then go from here, instead of visiting the classes?


def get_module_source_from_name(module_name: str) -> str:
    spec = importlib.util.find_spec(module_name)
    if spec is None or spec.origin is None:
        return f"Module {module_name} not found"

    with open(spec.origin, "r") as file:
        source_code = file.read()

    return source_code


from libcst import ClassDef, CSTTransformer, CSTVisitor
from libcst.metadata import MetadataWrapper, ParentNodeProvider


class ClassFinder(CSTVisitor):
    METADATA_DEPENDENCIES = (ParentNodeProvider,)

    def __init__(self, python_module):
        self.module = python_module
        self.classes = {}  # class LlamaAttentino
        self.imports = {}  # from flash_attn import
        self.function_def = {}  # def repeat_kv
        self.assignments = {}  # LLAMA_DOCSTRING
        self.protected_imports = {}  # if is_xxx_available()

    def visit_ClassDef(self, node: ClassDef) -> None:
        self.classes[node.name.value] = node

    def visit_SimpleStatementLine(self, node):
        match node:
            case cst.SimpleStatementLine(body=[cst.Assign(targets=[_], value=_)]):
                if isinstance(self.get_metadata(cst.metadata.ParentNodeProvider, node), cst.Module):
                    self.assignments[node.body[0]] = node
            case cst.SimpleStatementLine(body=[cst.Import(names=[_])]):
                self.imports[node.body[0].names] = node
            case cst.SimpleStatementLine(body=[cst.ImportFrom(_)]):
                self.imports[node.body[0].names] = node

    def visit_FunctionDef(self, node):
        if isinstance(self.get_metadata(cst.metadata.ParentNodeProvider, node), cst.Module):
            self.function_def[node.name.value] = node


class ReplaceNameTransformer(m.MatcherDecoratableTransformer):
    def __init__(self, old_name, new_name):
        super().__init__()
        self.new_name = new_name
        self.old_name = old_name
        self.regex = re.compile(re.escape(self.old_name), re.IGNORECASE)

    def preserve_case_replace(self, text):
        def replace(match):
            word = match.group()
            if word.isupper():
                return self.new_name.upper()
            elif word.istitle():
                return self.new_name.title()
            elif word.islower():
                return self.new_name.lower()
            else:
                return self.new_name.title()

        return self.regex.sub(replace, text)

    @m.leave(m.Name() | m.SimpleString() | m.Comment())
    def replace_name(self, original_node, updated_node):
        update = self.preserve_case_replace(updated_node.value)
        if update != original_node.value:
            print(f"changed: {updated_node.value} -> {update}")
        return updated_node.with_changes(value=self.preserve_case_replace(updated_node.value))


def find_classes_in_file(module, old_id="llama", new_id="gemma"):
    transformer = ReplaceNameTransformer(old_id, new_id)
    new_module = module.visit(transformer)

    new_module = MetadataWrapper(new_module)

    class_finder = ClassFinder(new_module)
    new_module.visit(class_finder)
    return class_finder


class DiffConverterTransformer(CSTTransformer):
    def __init__(self, python_module):
        super().__init__()
        self.python_module = python_module
        self.transformers_imports = {}
        self.transformers_mapping = {}
        self.class_mapping = {}
        self.visited_module = {}
        self.python_module = python_module

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if m.matches(node.module, m.Attribute()):
            full_statement = self.python_module.code_for_node(node.module)
            for imported_ in node.names:
                if "modeling_" in full_statement:
                    if full_statement not in self.transformers_imports:
                        source_code = get_module_source_from_name(full_statement)
                        tree = cst.parse_module(source_code)
                        self.transformers_imports[full_statement] = tree
                    self.transformers_mapping[self.python_module.code_for_node(imported_.name)] = full_statement

    def visit_Assign(self, node: cst.Assign) -> None:
        if m.matches(node.value, m.Name()):
            parent_package = self.transformers_mapping.get(node.value.value, None)
            if parent_package:
                if parent_package not in self.visited_module:
                    class_finder = find_classes_in_file(self.transformers_imports[parent_package])
                    self.visited_module[parent_package] = class_finder
                self.class_mapping[self.python_module.code_for_node(node)] = self.visited_module[
                    parent_package
                ].classes[node.targets[0].target.value]

    def leave_SimpleStatementLine(self, original_node: cst.Assign, updated_node: cst.CSTNode):
        match updated_node:
            # note: this is just a plain copy & paste of the pattern as seen in the CST
            case cst.SimpleStatementLine(body=[cst.Assign(targets=[_], value=_)]):
                assign = self.python_module.code_for_node(original_node.body[0])
                node = original_node.body[0]
                if m.matches(node.value, m.Name()) and assign in self.class_mapping:
                    return self.class_mapping[assign]
        return updated_node

    def leave_ClassDef(self, original_node: cst.Assign, updated_node):
        for base in original_node.bases:
            base_name = self.python_module.code_for_node(base)
            if base_name in self.transformers_mapping:
                super_classes = self.visited_module[self.transformers_mapping[base_name]].classes
                replacement_class = super_classes[updated_node.name.value]
                # Copy methods from original node to replacement node, preserving decorators
                updated_methods = {f.name.value: f for f in updated_node.body.body if isinstance(f, cst.FunctionDef)}
                replacement_methods = {
                    f.name.value: f for f in replacement_class.body.body if isinstance(f, cst.FunctionDef)
                }

                for name, func in updated_methods.items():
                    if name in replacement_methods:
                        # Replace the method in the replacement class
                        replacement_methods[name] = func

                # Rebuild the class body with updated methods
                new_body = [
                    replacement_methods.get(f.name.value, f) if isinstance(f, cst.FunctionDef) else f
                    for f in replacement_class.body.body
                ]

                new_replacement_class = replacement_class.with_changes(body=cst.IndentedBlock(body=new_body))

                # Ensure calls to `super()` in `__init__` are preserved
                new_replacement_class = (
                    cst.Module(body=[new_replacement_class]).visit(SuperTransformer(updated_methods)).body[0]
                )

                return new_replacement_class

        return updated_node

    def leave_Module(self, original_node: cst.Assign, node):
        new_body = []
        for visiter in self.visited_module.values():
            new_body += list(visiter.imports.values())
            new_body += list(visiter.assignments.values())
            new_body += list(visiter.function_def.values())

        return node.with_changes(body=[*new_body, *node.body])


class SuperTransformer(cst.CSTTransformer):
    def __init__(self, original_methods):
        self.original_methods = original_methods

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.CSTNode:
        if m.matches(updated_node, m.Call(func=m.Attribute(attr=m.Name("super")))):
            func_def = self.get_metadata(ParentNodeProvider, updated_node)
            if isinstance(func_def, cst.FunctionDef) and func_def.name.value in self.original_methods:
                # Replace super() calls in __init__ to ensure it references the correct class
                if func_def.name.value == "__init__":
                    return updated_node.with_changes(
                        args=[
                            cst.Arg(
                                value=cst.Call(
                                    func=cst.Name("super"), args=[cst.Arg(value=cst.Name(func_def.name.value))]
                                )
                            )
                        ]
                    )
        return updated_node

    def leave_Return(self, original_node: cst.Return, updated_node: cst.Return) -> cst.CSTNode:
        if m.matches(updated_node.value, m.Call(func=m.Attribute(attr=m.Name("super")))):
            func_def = self.get_metadata(ParentNodeProvider, updated_node)
            if isinstance(func_def, cst.FunctionDef) and func_def.name.value in self.original_methods:
                updated_return_value = updated_node.value.with_changes(
                    args=[
                        cst.Arg(
                            value=cst.Call(func=cst.Name("super"), args=[cst.Arg(value=cst.Name(func_def.name.value))])
                        )
                    ]
                )
                return updated_node.with_changes(value=updated_return_value)
        return updated_node


if __name__ == "__main__":
    # Parse the Python file
    with open("/Users/arthurzucker/Work/transformers/src/transformers/models/gemma/diff_gemma.py", "r") as file:
        code = file.read()
    module = cst.parse_module(code)

    transformers = DiffConverterTransformer(module)
    new_mod = module.visit(transformers)
    with open("/Users/arthurzucker/Work/transformers/src/transformers/models/gemma/modeling_gemma.py", "w") as f:
        f.write(new_mod.code)
    exit(0)