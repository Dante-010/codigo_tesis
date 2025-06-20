"""Module for the GraphEnviroment class"""
from src.community_algs.detection_algs import CommunityDetectionAlgorithm
from src.utils.utils import HyperParams, SimilarityFunctionsNames, Utils
from src.community_algs.metrics.similarity import CommunitySimilarity, GraphSimilarity
from typing import List, Tuple, Callable

from karateclub import Node2Vec

import networkx as nx
import numpy as np
import random
import time
import torch
import copy


class GraphEnvironment(object):
    """Enviroment where the agent will act, it will be a graph with a community"""

    def __init__(
        self,
        graph_path: str = HyperParams.GRAPH_NAME.value,
        community_detection_algorithm: str = HyperParams.DETECTION_ALG_NAME.value,
        beta: float = HyperParams.BETA.value,
        tau: float = HyperParams.TAU.value,
        community_similarity_function: str = SimilarityFunctionsNames.SOR.value,
        graph_similarity_function: str = SimilarityFunctionsNames.JAC_1.value,
    ) -> None:
        """Constructor for Graph Environment
        Parameters
        ----------
        graph_path : str, optional
            Path of the graph to load, by default HyperParams.GRAPH_NAME.value
        community_detection_algorithm : str
            Name of the community detection algorithm to use
        beta : float, optional
            Hyperparameter for the edge budget, value between 0 and 100
        tau : float, optional
            Strength of the deception constraint, value between 0 and 1, with 1
            we have a soft constraint, hard constraint otherwise, by default
            HyperParams.T.value
        community_similarity_function : str, optional
            Name of the community similarity function to use, by default
            SimilarityFunctionsNames.SOR.value
        graph_similarity_function : str, optional
            Name of the graph similarity function to use, by default
            SimilarityFunctionsNames.JAC_1.value
        """
        random.seed(time.time())
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

        # ° ---- GRAPH ---- ° #
        self.env_name = None
        self.graph = None
        self.original_graph = None
        self.old_graph = None
        self.n_connected_components = None
        self.set_graph(graph_path)

        # ° ---- NODE FEATURES ---- ° #
        # Set the node features of the graph, using Node2Vec
        self.embedding_model = None
        self.embedding = None
        # self.set_node_features()

        # ° ---- HYPERPARAMETERS ---- ° #
        assert beta >= 0 and beta <= 100, "Beta must be between 0 and 100"
        assert tau >= 0 and tau <= 1, "T value must be between 0 and 1"
        # Percentage of edges to remove
        self.beta = beta
        self.tau = tau
        # Weights for the reward and the penalty
        self.lambda_metric = None  # lambda_metric
        self.alpha_metric = None  # alpha_metric

        # ° ---- SIMILARITY FUNCTIONS ---- ° #
        self.community_similarity = None
        self.graph_similarity = None
        self.set_similarity_funtions(
            community_similarity_function, graph_similarity_function
        )

        # ° ---- COMMUNITY DETECTION ---- ° #
        self.detection_alg = None
        self.detection = None
        self.old_penalty_value = None
        self.original_community_structure = None
        self.old_community_structure = None
        self.new_community_structure = None
        self.prob_dist = None
        self.sorted_communities = None
        self.preferred_community_size = HyperParams.PREFERRED_COMMUNITY_SIZE.value[0]
        self.set_communities(community_detection_algorithm)

        # ° ---- COMMUNITY DECEPTION ---- ° #
        self.community_target = None
        self.node_target = None
        self.change_target_community()

        # ° ---- REWIRING STEP ---- ° #
        self.edge_budget = 0
        self.used_edge_budget = 0
        self.max_steps = 0
        self.stop_episode = False
        self.rewards = 0
        self.old_rewards = 0
        self.possible_actions = None
        self.len_add_actions = None
        self.set_rewiring_budget()

        # ° ---- PRINT ENVIRONMENT INFO ---- ° #
        # Print the environment information
        self.print_env_info()

    ############################################################################
    #                       EPISODE RESET FUNCTIONS                            #
    ############################################################################
    def reset(self, graph_reset=True) -> nx.Graph:
        """
        Reset the environment

        Parameters
        ----------
        graph_reset : bool, optional
            Whether to reset the graph to the original state, by default True

        Returns
        -------
        self.graph : nx.Graph
            Graph state after the reset, i.e. the original graph
        """
        self.used_edge_budget = 0
        self.stop_episode = False
        self.rewards = 0
        self.old_rewards = 0
        if graph_reset:
            self.graph = self.original_graph.copy()
        self.old_graph = None
        self.old_penalty_value = 0
        self.old_community_structure = self.original_community_structure
        self.possible_actions = self.get_possible_actions()
        return self.graph

    def change_target_node(self, node_target: int = None) -> None:
        """
        Change the target node to remove from the community

        Parameters
        ----------
        node_target : int, optional
            Node to remove from the community, by default None
        """
        if node_target is None:
            # Choose a node randomly from the community
            old_node = self.node_target
            while self.node_target == old_node:
                random.seed(time.time())
                self.node_target = random.choice(self.community_target)
        else:
            self.node_target = node_target

    def change_target_community(
        self, community: List[int] = None, node_target: int = None
    ) -> None:
        """
        Change the target community from which we want to hide the node

        Parameters
        ----------
        community : List[int]
            Community of node we want to remove from it
        node_target : int
            Node to remove from the community
        """
        if community is None:
            # ° METHOD 1: Select randomly a new community target different from the last one
            if HyperParams.COMMUNITY_CHANGE_METHOD.value == 1:
                self.random_community()
            # ° METHOD 2: Get the community with the highest number of nodes
            elif HyperParams.COMMUNITY_CHANGE_METHOD.value == 2:
                self.fixed_community()
            # ° METHOD 3: Choose the community based on the distribution of the number of nodes in the communities
            elif HyperParams.COMMUNITY_CHANGE_METHOD.value == 3:
                self.distribution_community()
            else:
                raise ValueError(
                    f"Invalid community change method: {HyperParams.COMMUNITY_CHANGE_METHOD.value}. Must be 1, 2, or 3."
                )
        else:
            self.community_target = community
        # Change the target node to remove from the community
        self.change_target_node(node_target=node_target)

    def random_community(self) -> None:
        """
        Choose a new community target randomly
        """
        old_community = self.community_target.copy()
        done = False
        while not done:
            random.seed(time.time())
            self.community_target = random.choice(
                self.original_community_structure.communities
            )
            # Check condition on new community
            if (
                len(self.community_target) > 1
                and self.community_target != old_community
            ) or len(self.original_community_structure.communities) < 2:
                done = True
        del old_community

    def fixed_community(self) -> None:
        """
        Choose the community with the length closest to the half of the maximum
        length of the communities.
        """
        communities = self.original_community_structure.communities
        communities_len = [len(c) for c in communities]
        preferred_size = int(
            np.ceil(max(communities_len) * self.preferred_community_size)
        )  # / 2
        closest = min(communities_len, key=lambda x: abs(x - preferred_size))
        self.community_target = communities[communities_len.index(closest)]

    def distribution_community(self, min_len: int = 10) -> None:
        """
        Choose a community based on the distribution of the number of nodes in
        the communities

        Parameters
        ----------
        min_len : int, optional
            Minimum size of the community, by default 10
        """
        communities = self.original_community_structure.communities
        # If the number of communities is less than 10, select a community randomly
        if len(communities) < 10:
            self.community_target = random.choice(communities)
            return
        # Compute the average number of nodes in the communities
        avg_nodes = np.mean([len(c) for c in communities])
        # Get the length of the community with the highest number of nodes
        max_nodes = max(len(community) for community in communities)

        # min_len = 0.4 * max_nodes
        max_len = 0.8 * max_nodes
        if avg_nodes < min_len:
            min_len = 2
        # Filter communities based on size constraint
        filtered_communities = [
            c
            for c in communities
            if len(c) > min_len and len(c) < max_len and len(c) < 5000
        ]
        # Randomly select a community from the filtered list
        self.community_target = random.choice(filtered_communities)

    ############################################################################
    #                      EPISODE STEP FUNCTIONS                              #
    ############################################################################
    def step(self, action: int) -> Tuple[nx.Graph, float, bool, bool]:
        """
        Step function for the environment

        Parameters
        ----------
        action : int
            Integer representing a node in the graph, it will be the destination
            node of the rewiring action (out source node is always the target node).

        Returns
        -------
        self.graph : nx.Graph
            Graph state after the action
        self.rewards : float
            Reward of the agent
        self.stop_episode : bool
            If the budget for the graph rewiring is exhausted, or the target
            node does not belong to the community anymore, the episode is finished
        done : bool
            Whether the episode is finished, if the target node does not belong
            to the community anymore, the episode is finished.
        """
        # ° ---- ACTION ---- ° #
        # Save the graph state before the action, used to compute the metrics
        self.old_graph = self.graph.copy()
        # Take action, add/remove the edge between target node and the model output
        budget_consumed = self.apply_action(action)
        # Set a negative reward if the action has not been applied
        if budget_consumed == 0:
            self.rewards = -1
            # The state is the same as before
            # return self.data_pyg, self.rewards, self.stop_episode
            return self.graph, self.rewards, self.stop_episode, False

        # ° ---- COMMUNITY DETECTION ---- ° #
        # Compute the community structure of the graph after the action
        self.new_community_structure = self.detection.compute_community(self.graph)

        # ° ---- REWARD ---- ° #
        self.rewards, done = self.get_reward()
        # If the target node does not belong to the community anymore,
        # the episode is finished
        if done:
            self.stop_episode = True

        # ° ---- BUDGET ---- ° #
        # Compute used budget
        self.used_edge_budget += budget_consumed
        # If the budget for the graph rewiring is exhausted, stop the episode
        if self.edge_budget - self.used_edge_budget < 1:
            self.stop_episode = True
            # If the budget is exhausted, and the target node still belongs to
            # the community, the reward is negative
            # if not done:
            #    self.rewards = -2

        self.old_community_structure = self.new_community_structure
        return self.graph, self.rewards, self.stop_episode, done

    def apply_action(self, action: int) -> int:
        """
        Applies the action to the graph, if there is an edge between the two
        nodes, it removes it, otherwise it adds it

        Parameters
        ----------
        action : int
            Integer representing a node in the graph, it will be the destination
            node of the rewiring action (out source node is always the target node).

        Returns
        -------
        budget_consumed : int
            Amount of budget consumed, 1 if the action has been applied, 0 otherwise
        """
        action = (self.node_target, action)
        # We need to take into account both the actions (u,v) and (v,u)
        action_reversed = (action[1], action[0])
        if action in self.possible_actions["ADD"]:
            self.graph.add_edge(*action, weight=1)
            self.possible_actions["ADD"].remove(action)
            return 1
        elif action_reversed in self.possible_actions["ADD"]:
            self.graph.add_edge(*action_reversed, weight=1)
            self.possible_actions["ADD"].remove(action_reversed)
            return 1
        elif action in self.possible_actions["REMOVE"]:
            self.graph.remove_edge(*action)
            self.possible_actions["REMOVE"].remove(action)
            return 1
        elif action_reversed in self.possible_actions["REMOVE"]:
            self.graph.remove_edge(*action_reversed)
            self.possible_actions["REMOVE"].remove(action_reversed)
            return 1
        return 0

    def act(self, action: int) -> Tuple[nx.Graph, bool]:
        """
        Function that is similar to the `step()` function but we do not compute
        the metrics and rewards.
        Indeed this function is used in the evaluation phase.

        Parameters
        ----------
        action : int
            Integer representing a node in the graph, it will be the destination
            node of the rewiring action (out source node is always the target node).

        Returns
        -------
        self.graph : nx.Graph
            Graph state after the action
        self.stop_episode : bool
            If the budget for the graph rewiring is exhausted, or the target
            node does not belong to the community anymore, the episode is finished
        """
        # ° ---- ACTION ---- ° #
        # Take action, add/remove the edge between target node and the model output
        budget_consumed = self.apply_action(action)
        if budget_consumed == -1:
            # ° ---- COMMUNITY DETECTION ---- ° #
            # Compute the community structure of the graph after the action
            self.new_community_structure = self.detection.compute_community(self.graph)
            # Check if the target node still belongs to the community
            new_community_target = next(
                (
                    c
                    for c in self.new_community_structure.communities
                    if self.node_target in c
                ),
                None,
            )
            # ° ---- COMMUNITY SIMILARITY ---- ° #
            # Remove target node from the communities, but first copy the lists
            # to avoid modifying them
            community_target_copy = self.community_target.copy()
            new_community_target.remove(self.node_target)
            community_target_copy.remove(self.node_target)
            # Compute the similarity between the new communities
            community_similarity = self.community_similarity(
                new_community_target,
                community_target_copy,
            )
            # Delete the copies
            del community_target_copy
            # ° ---------- REWARD ---------- ° #
            if community_similarity <= self.tau:
                self.stop_episode = True

        # ° ---- BUDGET ---- ° #
        # Compute used budget
        self.used_edge_budget += budget_consumed
        # If the budget for the graph rewiring is exhausted, stop the episode
        if self.edge_budget - self.used_edge_budget < 1:
            self.stop_episode = True

        return self.graph, self.stop_episode

    ############################################################################
    #                       SETTERS FUNCTIONS                                  #
    ############################################################################
    def set_graph(self, graph_path: str) -> None:
        """Set the graph of the environment"""
        # Load the graph from the dataset folder
        if graph_path is None:
            # Generate a synthetic graph
            graph, graph_path = Utils.generate_lfr_benchmark_graph()
        else:
            graph = Utils.import_mtx_graph(graph_path)

        graph = nx.convert_node_labels_to_integers(
            graph, first_label=0, ordering="sorted", label_attribute="node_type"
        )

        self.env_name = graph_path.split("/")[-1].split(".")[0]
        self.graph = self.set_node_features(graph)

        # Save the original graph to restart the rewiring process at each episode
        self.original_graph = self.graph.copy()
        # Save the graph state before the action, used to compute the metrics
        self.old_graph = None
        # Get the Number of connected components
        self.n_connected_components = nx.number_connected_components(self.graph)

    def set_node_features(self, graph) -> None:
        """Set the node features of the graph, using Node2Vec"""
        print("*" * 20, "Environment Information", "*" * 20)
        print("* Graph Name:", self.env_name)
        print("*", graph)
        if HyperParams.RANDOM_NODE2VEC.value:
            for node in graph.nodes():
                graph.nodes[node]["x"] = torch.rand(HyperParams.EMBEDDING_DIM.value)
        else:
            print("* * Compute Node Embedding using Node2Vec for nodes features")
            print("* * ...")
            # Build node features using Node2Vec, set the embedding dimension to 128.
            self.embedding_model = Node2Vec(
                walk_number=HyperParams.WALK_NUMBER.value,
                walk_length=HyperParams.WALK_LENGTH.value,
                dimensions=HyperParams.EMBEDDING_DIM.value,
            )
            self.embedding_model.fit(graph)
            print("* * End Embedding Computation")
            self.embedding = self.embedding_model.get_embedding()
            # Add the embedding to the graph
            for node in graph.nodes():
                graph.nodes[node]["x"] = torch.tensor(self.embedding[node])

        for edge in graph.edges():
            if "weight" not in graph.edges[edge]:
                # Add weight to the edges
                graph.edges[edge]["weight"] = 1
        return graph

    def set_similarity_funtions(
        self, community_similarity_function: str, graph_similarity_function: str
    ) -> None:
        """
        Set the similarity functions to use to compare the communities and
        the graphs
        """
        # Select the similarity function to use to compare the communities
        self.community_similarity = CommunitySimilarity(
            community_similarity_function
        ).select_similarity_function()
        self.graph_similarity = GraphSimilarity(
            graph_similarity_function
        ).select_similarity_function()

    def set_communities(self, community_detection_algorithm) -> None:
        """
        Set the community detection algorithm to use, and compute the community
        structure of the graph before the deception actions.
        """
        self.detection_alg = community_detection_algorithm
        # Community Algorithms objects
        self.detection = CommunityDetectionAlgorithm(community_detection_algorithm)
        # Metrics
        self.old_penalty_value = 0
        # Compute the community structure of the graph, before the action,
        # i.e. before the deception
        self.original_community_structure = self.detection.compute_community(self.graph)
        # ! It is a NodeClustering object
        self.old_community_structure = self.original_community_structure

        # Compute probability distribution of the number of nodes in the communities
        communities = copy.deepcopy(self.original_community_structure.communities)
        self.sorted_communities = sorted(communities, key=len)
        # Get the number of nodes in each community
        n_nodes = [len(community) for community in self.sorted_communities]
        # Compute the probability distribution
        self.prob_dist = [n_node / sum(n_nodes) for n_node in n_nodes]
        for i, community in enumerate(self.sorted_communities):
            if len(community) <= 1:
                self.sorted_communities.pop(i)
                self.prob_dist.pop(i)
        assert (
            len(self.sorted_communities) > 0
        ), "No communities with more than one node"

    def set_preferred_community_size(self, preferred_community_size: int) -> None:
        """
        Set the preferred community size, to extract the community with the
        right dimension, i.e. the community with the number of nodes closest
        to the preferred size.

        Parameters
        ----------
        preferred_community_size :
            Percentage of the maximum community size
        """
        self.preferred_community_size = preferred_community_size

    def set_rewiring_budget(self) -> None:
        """Set the rewiring budget for the graph, and the valid actions"""
        # Compute the action budget for the graph
        # TEST, use directly the beta parameter
        # self.edge_budget = self.beta
        self.edge_budget = self.get_edge_budget()
        # Amount of budget used
        self.used_edge_budget = 0
        # Max Rewiring Steps during an episode, set a limit to avoid infinite
        # episodes in case the agent does not find the target node
        self.max_steps = (
            self.edge_budget * HyperParams.MAX_STEPS_MUL.value
        )  # self.graph.number_of_edges()
        # Whether the budget for the graph rewiring is exhausted, or the target
        # node does not belong to the community anymore
        self.stop_episode = False
        self.rewards = 0
        # Reward of the previous step
        self.old_rewards = 0
        # Compute the set of possible actions
        self.possible_actions = self.get_possible_actions()
        # Length of the list of possible actions to add
        self.len_add_actions = len(self.possible_actions["ADD"])

    ############################################################################
    #                       GETTERS FUNCTIONS                                  #
    ############################################################################

    def get_edge_budget(self) -> int:
        """
        Computes the edge budget for each graph

        Returns
        -------
        int
            Edge budgets of the graph
        """
        # TEST: Three different ways to compute the edge budget

        # 1. Mean degree of the graph times the parameter beta
        if self.env_name == "pow" or self.env_name == "kar":
            return int(
                (self.graph.number_of_edges() / self.graph.number_of_nodes() + 1)
                * self.beta
            )

        return int(
            self.graph.number_of_edges() / self.graph.number_of_nodes() * self.beta
        )

        # 2. Percentage of edges of the whole graph
        # return int(math.ceil((self.graph.number_of_edges() * self.beta / 100)))

        # 3. Percentage of edges of the whole graph divided by the number of nodes in the community
        # return int(math.ceil((self.graph.number_of_edges() * self.beta / 100) / len(self.community_target)))

    def get_penalty(self) -> float:
        """
        Compute the metrics and return the penalty to subtract from the reward

        Returns
        -------
        penalty: float
            Penalty to subtract from the reward
        """
        # ° ---- COMMUNITY DISTANCE ---- ° #
        community_distance = self.new_community_structure.normalized_mutual_information(
            self.old_community_structure
        ).score
        # In NMI 1 means that the two community structures are identical,
        # 0 means that they are completely different
        # We want to maximize the NMI, so we subtract it from 1
        community_distance = 1 - community_distance
        # ° ---- GRAPH DISTANCE ---- ° #
        graph_distance = self.graph_similarity(self.graph, self.old_graph)
        # ° ---- PENALTY ---- ° #
        assert (
            self.alpha_metric is not None
        ), "Alpha metric is None, must be set in grid search"
        penalty = (
            self.alpha_metric * community_distance
            + (1 - self.alpha_metric) * graph_distance
        )
        # Subtract the metric value of the previous step
        penalty -= self.old_penalty_value
        # Update with the new values
        self.old_penalty_value = penalty
        return penalty

    def get_reward(self) -> Tuple[float, bool]:
        """
        Computes the reward for the agent, it is a 0-1 value function, if the
        target node still belongs to the community, the reward is 0 minus the
        penalty, otherwise the reward is 1 minus the penalty.

        As new community target after the action, we consider the community
        that contains the target node, if this community satisfies the deception
        constraint, the episode is finished, otherwise not.

        Returns
        -------
        reward : float
            Reward of the agent
        done : bool
            Whether the episode is finished, if the target node does not belong
            to the community anymore, the episode is finished
        """
        assert (
            self.lambda_metric is not None
        ), "Lambda metric is None, must be set in grid search"
        new_community_target = next(
            (
                c
                for c in self.new_community_structure.communities
                if self.node_target in c
            ),
            None,
        )
        assert new_community_target is not None, "New community target is None"
        # ° ---------- PENALTY ---------- ° #
        # Compute the metric to subtract from the reward
        penalty = self.get_penalty()
        # If the target node does not belong to the community anymore,
        # the episode is finished
        if len(new_community_target) == 1:
            reward = 1 - (self.lambda_metric * penalty)
            return reward, True
        # ° ---- COMMUNITY SIMILARITY ---- ° #
        # Remove target node from the communities, but first copy the lists
        # to avoid modifying them
        new_community_target_copy = new_community_target.copy()
        new_community_target_copy.remove(self.node_target)
        community_target_copy = self.community_target.copy()
        community_target_copy.remove(self.node_target)
        # Compute the similarity between the new communities
        community_similarity = self.community_similarity(
            new_community_target_copy,
            community_target_copy,
        )
        # Delete the copies
        del new_community_target_copy, community_target_copy
        # ° ---------- REWARD ---------- ° #
        if community_similarity <= self.tau:
            # We have reached the deception constraint, the episode is finished
            reward = 1 - (self.lambda_metric * penalty)
            return reward, True
        reward = 0 - (self.lambda_metric * penalty)
        return reward, False

    def get_possible_actions(self) -> dict:
        """
        Returns all the possible actions that can be applied to the graph
        given a source node (self.node_target). The possible actions are:
            - Add an edge between the source node and a node outside the community
            - Remove an edge between the source node and a node inside the community

        Returns
        -------
        self.possible_actions : dict
            Dictionary containing the possible actions that can be applied to
            the graph. The dictionary has two keys: "ADD" and "REMOVE", each
            key has a list of tuples as value, where each tuple is an action.
        """
        possible_actions = {"ADD": set(), "REMOVE": set()}
        # Helper functions to check if a node is in/out-side the community

        def in_community(node):
            return node in self.community_target

        def out_community(node):
            return node not in self.community_target

        u = self.node_target
        for v in self.graph.nodes():
            if u == v:
                continue
            # We can remove an edge iff both nodes are in the community
            if in_community(u) and in_community(v):
                if self.graph.has_edge(u, v):
                    if (v, u) not in possible_actions["REMOVE"]:
                        possible_actions["REMOVE"].add((u, v))
            # We can add an edge iff one node is in the community and the other is not
            elif (in_community(u) and out_community(v)) or (
                out_community(u) and in_community(v)
            ):
                # Check if there is already an edge between the two nodes
                if not self.graph.has_edge(u, v):
                    if (v, u) not in possible_actions["ADD"]:
                        possible_actions["ADD"].add((u, v))
        return possible_actions

    ############################################################################
    #                           ENVIRONMENT INFO                               #
    ############################################################################
    def print_env_info(self) -> None:
        """Print the environment information"""
        print("* Community Detection Algorithm:", self.detection_alg)
        print(
            "* Number of communities found:",
            len(self.original_community_structure.communities),
        )
        # print("* Rewiring Budget:", self.edge_budget, "=", self.beta, "*", self.graph.number_of_edges(), "/ 100",)
        print(
            "* BETA - Rewiring Budget: (n_edges/n_nodes)*BETA =",
            self.graph.number_of_edges(),
            "/",
            self.graph.number_of_nodes(),
            "*",
            self.beta,
            "=",
            int(self.graph.number_of_edges() / self.graph.number_of_nodes())
            * self.beta,
        )
        print("* TAU - Weight of the Deception Constraint:", self.tau)
        print("*", "-" * 58, "\n")
