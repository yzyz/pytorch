#include <torch/csrc/jit/passes/create_autodiff_subgraphs.h>

#include <c10/util/Exception.h>
#include <torch/csrc/jit/ir/alias_analysis.h>
#include <torch/csrc/jit/ir/ir.h>
#include <torch/csrc/jit/jit_log.h>
#include <torch/csrc/jit/passes/canonicalize.h>
#include <torch/csrc/jit/passes/common_subexpression_elimination.h>
#include <torch/csrc/jit/passes/remove_redundant_profiles.h>
#include <torch/csrc/jit/passes/utils/subgraph_utils.h>
#include <torch/csrc/jit/runtime/autodiff.h>

namespace torch {
namespace jit {

namespace {

struct WorkBlock : public std::pair<Node*, Node*> {
  using pair::pair;

  Node* begin() {
    return this->first;
  }
  Node* end() {
    return this->second;
  }
};

class SubgraphSlicer {
 public:
  SubgraphSlicer(
      Block* block,
      std::shared_ptr<Graph> graph,
      size_t minSubgraphSize,
      AliasDb& aliasDb,
      std::vector<Node*>& diff_nodes)
      : block_(block),
        graph_(std::move(graph)),
        minSubgraphSize_(minSubgraphSize),
        aliasDb_(aliasDb),
        diff_nodes_(diff_nodes) {}

  void run() {
    // We maintain alias db correctness in-place while building up the autodiff
    // subgraphs, however it is difficult to preserve correctness when
    // un-inlining autodiff subgraphs. We first recursively construct all
    // subgraphs and then recursively cleanup & unmerge the small subgraphs
    buildupSubgraphs();
    GRAPH_DUMP("before unfuseAliasedOutputs", graph_);
    unfuseAliasedOutputs(block_);
    cleanupSubgraphs();
    // Run CSE globally onceto eliminate duplicates that may have occurred
    // while inlining subgraphs.
    EliminateCommonSubexpression(graph_);
  }

  void cleanupSubgraphs() {
    auto curNode = *block_->nodes().rbegin();
    while (curNode != *block_->nodes().rend()) {
      // Save the previous node, since we might delete `curNode` in next block
      auto prevNode = curNode->prev();
      if (curNode->kind() == prim::DifferentiableGraph) {
        // Inlining nodes may cause some subexpression to come back in the
        // subgraphs (for example, copying constants in repeatedly will generate
        // redundant prim::Constants). Run CSE to clean them up.
        EliminateCommonSubexpression(curNode->g(attr::Subgraph));

        if (!inlineIfTooSmall(curNode)) {
          diff_nodes_.push_back(curNode);
        }
      }
      curNode = prevNode;
    }

    for (Node* n : block_->nodes()) {
      for (Block* b : n->blocks()) {
        SubgraphSlicer(b, graph_, minSubgraphSize_, aliasDb_, diff_nodes_)
            .cleanupSubgraphs();
      }
    }
  }

  void buildupSubgraphs() {
    // We need to run the slicer multiple times in order to get all merge
    // opportunities. This is because moveBeforeTopologicalValid may reorder
    // nodes to be AFTER the current iteration point. In order to properly
    // consider those nodes for merging, we need run the pass until no changes
    // have been made.
    //
    // Example:
    //   c = f(a, b)
    //   d = f(c)
    //   e = f(d)  <- iter is here, moving upward
    // After c.moveBeforeTopologicallyValid(e), we have:
    //   c = f(a, b)
    //   e = f(d)  <- iter still here
    //   d = f(c)  <- this was node moved on the other side.

    // see [workblocks]
    auto workblocks = buildWorkBlocks();
    for (auto& workblock : workblocks) {
      bool any_changed = true;
      while (any_changed) {
        any_changed = false;
        for (auto it = workblock.end()->reverseIterator();
             it != workblock.begin()->reverseIterator();) {
          // NOLINTNEXTLINE(cppcoreguidelines-init-variables)
          bool changed;
          std::tie(it, changed) = scanNode(*it);
          any_changed |= changed;
        }
      }
    }

    // Construct Subgraphs Recursively
    for (Node* n : block_->nodes()) {
      for (auto subBlock : n->blocks()) {
        SubgraphSlicer(
            subBlock, graph_, minSubgraphSize_, aliasDb_, diff_nodes_)
            .buildupSubgraphs();
      }
    }
  }

 private:
  void unfuseAliasedOutputs(Block* b) {
    bool any_changed = true;
    while (any_changed) {
      any_changed = false;
      // we walk in the reverse order, so we can skip
      // nodes that might get unfused after the current
      // prim::DifferentiableGraph
      for (auto n : b->nodes().reverse()) {
        if (n->kind() == prim::DifferentiableGraph) {
          // aliased outputs in DifferentiableGraphs must be unfused
          // since autodiff doesn't know how to handle them correctly
          // N.B. Note, |= since we don't want `unfuseAliasedOutputs`
          // to short-circuit
          any_changed |= SubgraphUtils::unmergeAliasedOutputs(n);
          any_changed |= SubgraphUtils::unmergeOutputsAlisingInputs(n);
          GRAPH_DEBUG(
              "any_changed on ",
              any_changed,
              " ",
              n->g(attr::Subgraph)->toString(false));
        }
      }
    }

    for (Node* n : b->nodes()) {
      for (Block* ib : n->blocks()) {
        unfuseAliasedOutputs(ib);
      }
    }
  }

  std::vector<WorkBlock> buildWorkBlocks() {
    // [workblocks]
    // the IR has many nodes which can never be reordered around, such as a
    // prim::Bailout. if a node N is surrounded by two nodes which cannot be
    // reordered, A and B, then a differentiable subgraph that is created from N
    // can only contain nodes from (A, B) The nodes from A to B represent one
    // work block for the subgraph slicer to work on. By creating these up
    // front, we avoid retraversing the whole graph block any time scanNode
    // returns, and we can also avoid attempting to create differentiable
    // subgraphs in work blocks that do not contain a # of differentiable nodes
    // >= minSubgraphSize_

    Node* end_bound_node = block_->return_node();
    Node* curr = end_bound_node->prev();

    std::vector<WorkBlock> worklist;
    size_t differentiable_nodes = 0;

    while (curr != block_->param_node()) {
      differentiable_nodes += shouldConsiderForMerge(curr);

      // cannot reorder around side effectful nodes
      if (curr->hasSideEffects()) {
        // not enough differentiable nodes to create a differentiable subgraph
        if (differentiable_nodes >= minSubgraphSize_) {
          worklist.emplace_back(curr, end_bound_node);
        }
        differentiable_nodes = 0;
        end_bound_node = curr;
      }
      curr = curr->prev();
    }

    if (differentiable_nodes >= minSubgraphSize_) {
      worklist.emplace_back(curr, end_bound_node);
    }

    return worklist;
  }

  // Inline this node's group subgraph into the outer graph if it's smaller
  // than the specified minimum size.
  //
  // Returns true if an inlining has occurred, false otherwise.
  bool inlineIfTooSmall(Node* n) {
    AT_ASSERT(n->kind() == prim::DifferentiableGraph);
    auto subgraph = SubgraphUtils::getSubgraph(n);
    size_t i = 0;
    for (auto it = subgraph->nodes().begin(); it != subgraph->nodes().end();
         ++it) {
      i += !it->notExecutedOp();
      if (i >= minSubgraphSize_) {
        return false;
      }
    }

    SubgraphUtils::unmergeSubgraph(n);
    return true;
  }

  value_list sortReverseTopological(ArrayRef<Value*> inputs) {
    value_list result;
    for (auto i : inputs) {
      if (i->node()->owningBlock() == block_) {
        result.push_back(i);
      }
    }
    // Sort in reverse topological order
    std::sort(result.begin(), result.end(), [&](Value* a, Value* b) {
      return a->node()->isAfter(b->node());
    });
    return result;
  }

  bool isViewOp(Node* n) {
    switch (n->kind()) {
      case aten::view:
      case aten::view_as:
      case aten::reshape:
      case aten::reshape_as:
      case aten::transpose:
      case aten::expand:
      case aten::expand_as:
        return true;
    }
    return false;
  }

  bool shouldConsiderForMerge(Node* node) {
    // if we're already in the process of merging
    if (node->kind() == prim::DifferentiableGraph) {
      return true;
    }
    if (node->kind() == prim::Constant) {
      return false;
    }

    // view ops as outputs of differentiable subgraphs can cause incorrect
    // differentiation for now, do not include them in the subgraph
    if (isViewOp(node)) {
      return false;
    }

    return isDifferentiable(node);
  }

  std::pair<graph_node_list::iterator, bool> scanNode(Node* consumer) {
    if (shouldConsiderForMerge(consumer)) {
      if (consumer->kind() != prim::DifferentiableGraph) {
        consumer = SubgraphUtils::createSingletonSubgraphAndUpdateAliasing(
            consumer, prim::DifferentiableGraph, aliasDb_);
      }
      auto inputs = sortReverseTopological(consumer->inputs());
      for (auto input : inputs) {
        if (auto group = tryMerge(consumer, input->node())) {
          // we successfully merged, so the new group's `inputs` may have
          // changed. So rescan the new group for more merging opportunities.
          return std::make_pair(group.value()->reverseIterator(), true);
        }
      }
    }

    return std::make_pair(++consumer->reverseIterator(), false);
  }

  // Try to merge `producer` into `consumer`. If successful, this destroys
  // `producer` and returns the `consumer` group.
  c10::optional<Node*> tryMerge(Node* consumer, Node* producer) {
    AT_ASSERT(consumer->kind() == prim::DifferentiableGraph);
    bool canMerge = shouldConsiderForMerge(producer) &&
        aliasDb_.moveBeforeTopologicallyValid(producer, consumer);

    if (!canMerge) {
      return c10::nullopt;
    }

    SubgraphUtils::mergeNodeIntoSubgraphAndUpdateAliasing(
        producer, consumer, aliasDb_);
    return consumer;
  }

  Block* block_;
  std::shared_ptr<Graph> graph_;
  size_t minSubgraphSize_;
  AliasDb& aliasDb_;
  std::vector<Node*>& diff_nodes_;
};

c10::optional<bool> getProfileNodeRequiresGrad(Node* n) {
  TORCH_INTERNAL_ASSERT(n->kind() == prim::profile);
  if (!n->hasAttribute(attr::profiled_type)) {
    return c10::nullopt;
  }
  auto& type = n->ty(attr::profiled_type);
  if (type->castRaw<TensorType>() == nullptr) {
    return c10::nullopt;
  }
  return type->expectRef<TensorType>().requiresGrad();
}

void AddRequiresGradToDifferentiableGraph(Node* diff_graph) {
  TORCH_INTERNAL_ASSERT(diff_graph->kind() == prim::DifferentiableGraph);
  const auto& subgraph = diff_graph->g(attr::Subgraph);
  for (auto i : c10::irange(subgraph->outputs().size())) {
    Value* output = subgraph->outputs()[i];
    if (output->node()->kind() == prim::profile) {
      // already have requires_grad info from this profile node
      continue;
    }
    if (output->type()->castRaw<TensorType>() == nullptr) {
      // non-tensors don't get profiled.
      continue;
    }
    if (output->type()->expectRef<TensorType>().requiresGrad().has_value()) {
      continue;
    }

    // this node doesn't have any requires_grad info.
    // look at its uses to try to find a profile node.
    c10::optional<bool> requiresGrad = c10::nullopt;
    for (auto& use : diff_graph->output(i)->uses()) {
      if (use.user->kind() == prim::profile) {
        c10::optional<bool> req_grad_use;
        if ((req_grad_use = getProfileNodeRequiresGrad(use.user)).has_value()) {
          requiresGrad = req_grad_use;
          break;
        }
      }

      // maybe the profile node got absorbed into a differentiable graph
      if (use.user->kind() == prim::DifferentiableGraph) {
        const auto& dg = use.user->g(attr::Subgraph);
        // check all the uses of this graph input to look for profile nodes.
        Value* dg_value = dg->inputs()[use.offset];
        for (auto& dg_use : dg_value->uses()) {
          if (dg_use.user->kind() == prim::profile) {
            c10::optional<bool> req_grad_use;
            if ((req_grad_use = getProfileNodeRequiresGrad(dg_use.user))
                    .has_value()) {
              requiresGrad = req_grad_use;
              break;
            }
          }
        }
        if (requiresGrad) {
          break;
        }
      }
    }

    if (requiresGrad.has_value()) {
      output->setType(output->type()->expectRef<TensorType>().withRequiresGrad(
          requiresGrad));
    }
  }
}

// autodiff.cpp needs to know, for each output, whether or not it requires
// grad. Sometimes a profile node will be present on the output, but sometimes
// it won't be present. This might happen if there's a node with side effects
// in between the definition of the output node and the profile node; in this
// case the profile node and output node would be in different workblocks and
// couldn't be merged into the same DifferentiableGraph. (see [workblocks])
// Or it could happen if the output is profiled twice and the profile nodes get
// removed by unfusedAliasedOutputs.
void AddRequiresGradOnOutputNodes(Block* block) {
  for (Node* n : block->nodes()) {
    if (n->kind() == prim::DifferentiableGraph) {
      AddRequiresGradToDifferentiableGraph(n);
    }
    for (Block* b : n->blocks()) {
      AddRequiresGradOnOutputNodes(b);
    }
  }
}
} // anonymous namespace

std::vector<Node*> CreateAutodiffSubgraphs(
    const std::shared_ptr<Graph>& graph,
    size_t threshold) {
  std::vector<Node*> diff_nodes;
  AliasDb db(graph);
  GRAPH_DEBUG("Before creating autodiff subgraphs", *graph);
  SubgraphSlicer(graph->block(), graph, threshold, db, diff_nodes).run();
  GRAPH_DEBUG("After creating autodiff subgraphs", *graph);
  AddRequiresGradOnOutputNodes(graph->block());
  GRAPH_DEBUG("diff_nodes.size() ", diff_nodes.size());
  return diff_nodes;
}
} // namespace jit
} // namespace torch
