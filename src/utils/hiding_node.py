from re import S
from src.utils.utils import HyperParams, Utils, FilePaths
from src.environment.graph_env import GraphEnvironment
from src.agent.agent import Agent

from src.community_algs.baselines.node_hiding.random_hiding import RandomHiding
from src.community_algs.baselines.node_hiding.degree_hiding import DegreeHiding
from src.community_algs.baselines.node_hiding.roam_hiding import RoamHiding
from src.community_algs.baselines.node_hiding.centrality_hiding import CentralityHiding
from src.community_algs.baselines.node_hiding.greedy_hiding import GreedyHiding

from typing import List, Callable, Tuple
from tqdm import trange
import multiprocessing
import networkx as nx
import cdlib
import time
import copy


class NodeHiding:
    """
    Class to evaluate the performance of the agent on the Node Hiding task, and
    compare it with the baseline algorithms:
        - Random Hiding: choose randomly the edges to remove/add
        - Degree Hiding: choose the edges to remove/add based on the degree
        - Roam Heuristic: use roam heuristic
    """

    def __init__(
        self,
        agent: Agent,
        model_path: str,
        lr: float = HyperParams.LR_EVAL.value,
        gamma: float = HyperParams.GAMMA_EVAL.value,
        lambda_metric: float = HyperParams.LAMBDA_EVAL.value,
        alpha_metric: float = HyperParams.ALPHA_EVAL.value,
        epsilon_prob: float = HyperParams.EPSILON_EVAL.value,
        eval_steps: int = HyperParams.STEPS_EVAL.value,
    ) -> None:
        self.agent = agent
        self.original_graph = agent.env.original_graph.copy()
        self.model_path = model_path
        self.env_name = agent.env.env_name
        self.detection_alg = agent.env.detection_alg
        self.community_target = agent.env.community_target

        # Copy the community structure to avoid modifying the original one
        self.community_structure = copy.deepcopy(agent.env.original_community_structure)
        self.node_target = agent.env.node_target

        self.lr = lr
        self.gamma = gamma
        self.lambda_metric = lambda_metric
        self.alpha_metric = alpha_metric
        self.epsilon_prob = epsilon_prob
        self.eval_steps = eval_steps

        self.beta = None
        self.tau = None
        self.edge_budget = None
        self.max_steps = None

        # HyperParams.ALGS_EVAL.value
        self.evaluation_algs = [
            "Agent",
            "Random",
            "Degree",
            "Roam",
            "Centrality",
            "Greedy",
        ]
        # TEST
        # self.evaluation_algs = ["Greedy"]

    def set_parameters(self, beta: int, tau: float) -> None:
        """Set the environment with the new parameters, for new experiments

        Parameters
        ----------
        beta : int
            Multiplicative factor for the number of edges to remove/add
        tau : float
            Constraint on the goal achievement
        """
        self.beta = beta
        self.tau = tau

        self.agent.env.beta = beta
        self.agent.env.tau = tau
        self.agent.env.set_rewiring_budget()

        self.edge_budget = self.agent.env.edge_budget
        if self.edge_budget < 1:
            raise ValueError("Edge budget must be greater than 1")
        self.max_steps = self.agent.env.max_steps

        # Initialize the log dictionary
        self.set_log_dict()

        self.path_to_save = (
            FilePaths.TEST_DIR.value
            + f"{self.env_name}/{self.detection_alg}/"
            + f"node_hiding/"
            + f"tau_{self.tau}/"
            + f"beta_{self.beta}/"
            # + f"eps_{self.epsilon_prob}/"
            # + f"lr_{self.lr}/gamma_{self.gamma}/"
            # + f"lambda_{self.lambda_metric}/alpha_{self.alpha_metric}/"
        )

    def reset_experiment(self, target_community=True) -> None:
        """
        Reset the environment and the agent at the beginning of each episode,
        and change the target community and node

        Parameters
        ----------
        target_community : bool, optional
            If True, change the target community, by default True
        """
        if target_community:
            self.agent.env.change_target_community()
            # Copy the community target to avoid modifying the original one
            self.community_target = copy.deepcopy(self.agent.env.community_target)
        else:
            self.agent.env.change_target_node()
        self.node_target = self.agent.env.node_target

        # Baseline algorithms
        self.random_hiding = RandomHiding(
            env=self.agent.env,
            steps=self.edge_budget,
            target_community=self.community_target,
        )
        self.degree_hiding = DegreeHiding(
            env=self.agent.env,
            steps=self.edge_budget,
            target_community=self.community_target,
        )
        self.roam_hiding = RoamHiding(
            self.original_graph, self.node_target, self.edge_budget, self.detection_alg
        )

        self.centrality_hiding = CentralityHiding(
            env=self.agent.env,
            steps=self.edge_budget,
            target_community=self.community_target,
        )

        self.greedy_hiding = GreedyHiding(
            env=self.agent.env,
            steps=self.edge_budget,
            target_community=self.community_target,
        )

    ############################################################################
    #                               EVALUATION                                 #
    ############################################################################
    def run_experiment(self):
        """
        Function to run the evaluation of the agent on the Node Hiding task,
        and compare it with the baseline algorithms
        """
        # Start evaluation
        if HyperParams.COMMUNITY_CHANGE_METHOD.value == 2:
            preferred_size_list = HyperParams.PREFERRED_COMMUNITY_SIZE.value
        else:
            preferred_size_list = [self.agent.env.preferred_community_size]
        sizes = trange(
            len(preferred_size_list), desc="* * * Community Size", leave=True
        )
        for i in sizes:
            # Change the community size at each episode
            self.agent.env.preferred_community_size = preferred_size_list[i]
            # print("* Community Size:", self.agent.env.preferred_community_size)
            # Change the target community
            self.reset_experiment()

            sizes.set_description(f"* * * Community Size {len(self.community_target)}")
            steps = trange(self.eval_steps, desc="Testing Episode", leave=False)
            for step in steps:
                # print("* Node Target:", self.node_target)
                # print("* Community Target Length:", len(self.community_target))
                # print("* Edge Budget:", self.edge_budget)

                # Change target node within the community
                self.reset_experiment(target_community=False)

                # ° ------ Agent Rewiring ------ ° #
                steps.set_description(
                    f"* * * Testing Episode {step+1} | Agent Rewiring"
                )
                self.run_alg(self.run_agent)

                # ° ------   Baselines   ------ ° #
                # Random Rewiring
                steps.set_description(
                    f"* * * Testing Episode {step+1} | Random Rewiring"
                )
                self.run_alg(self.run_random)

                # Degree Rewiring
                steps.set_description(
                    f"* * * Testing Episode {step+1} | Degree Rewiring"
                )
                self.run_alg(self.run_degree)

                # Roam Rewiring
                steps.set_description(f"* * * Testing Episode {step+1} | Roam Rewiring")
                self.run_alg(self.run_roam)
                # compute_baselines = False

                steps.set_description(
                    f"* * * Testing Episode {step+1} | Centrality Rewiring"
                )
                self.run_alg(self.run_centrality)

                steps.set_description(
                    f"* * * Testing Episode {step+1} | Greedy Rewiring"
                )
                self.run_alg(self.run_greedy)

        Utils.check_dir(self.path_to_save)
        Utils.save_test(
            log=self.log_dict,
            files_path=self.path_to_save,
            log_name="evaluation_node_hiding",
            algs=self.evaluation_algs,
            metrics=["nmi", "goal", "time", "steps"],
        )

    # Define a function to run each algorithm
    def run_alg(self, function: Callable) -> None:
        """
        Wrapper function to run the evaluation of a generic algorithm

        Parameters
        ----------
        function : Callable
            Algorithm to evaluate
        """
        start = time.time()
        alg_name, new_communities, step = function()
        end = time.time() - start

        # Compute NMI
        nmi = self.get_nmi(self.community_structure, new_communities)

        # Check if the goal was achieved
        community_target = self.get_new_community(new_communities)
        goal = self.check_goal(community_target)

        # Save results in the log dictionary
        self.save_metrics(alg_name, goal, nmi, end, step)

    ############################################################################
    #                               AGENT                                      #
    ############################################################################
    def run_agent(self) -> Tuple[str, cdlib.NodeClustering, int]:
        """
        Evaluate the agent on the Node Hiding task

        Returns
        -------
        Tuple[str, cdlib.NodeClustering, int]:
            Algorithm name, Set of new communities, steps
        """
        new_graph = self.agent.test(
            lr=self.lr,
            gamma=self.gamma,
            lambda_metric=self.lambda_metric,
            alpha_metric=self.alpha_metric,
            epsilon_prob=self.epsilon_prob,
            model_path=self.model_path,
        )

        # Compute the new community structure
        self.agent.env.new_community_structure = (
            self.agent.env.detection.compute_community(new_graph)
        )

        return (
            "Agent",
            self.agent.env.new_community_structure,
            self.agent.env.used_edge_budget,
        )

    ############################################################################
    #                               BASELINES                                  #
    ############################################################################
    def run_random(self) -> Tuple[str, cdlib.NodeClustering, int]:
        """
        Evaluate the Random Hiding algorithm on the Node Hiding task

        Returns
        -------
        Tuple[str, cdlib.NodeClustering, int]:
            Algorithm name, Set of new communities, steps
        """
        (
            rh_graph,
            rh_communities,
            steps,
        ) = self.random_hiding.hide_target_node_from_community()
        return "Random", rh_communities, steps

    def run_degree(self) -> Tuple[str, cdlib.NodeClustering, int]:
        """
        Evaluate the Degree Hiding algorithm on the Node Hiding task

        Returns
        -------
        Tuple[str, cdlib.NodeClustering, int]:
            Algorithm name, Set of new communities, steps
        """
        (
            dh_graph,
            dh_communities,
            steps,
        ) = self.degree_hiding.hide_target_node_from_community()
        return "Degree", dh_communities, steps

    def run_roam(self) -> Tuple[str, cdlib.NodeClustering, int]:
        """
        Evaluate the Roam Hiding algorithm on the Node Hiding task

        Returns
        -------
        Tuple[str, cdlib.NodeClustering, int]:
            Algorithm name, Set of new communities, steps
        """
        ro_graph, ro_communities = self.roam_hiding.roam_heuristic(self.edge_budget)
        return "Roam", ro_communities, self.edge_budget

    def run_centrality(self) -> Tuple[str, cdlib.NodeClustering, int]:
        """
        Evaluate the Roam Hiding algorithm on the Node Hiding task

        Returns
        -------
        Tuple[str, cdlib.NodeClustering, int]:
            Algorithm name, Set of new communities, steps
        """
        (
            ch_graph,
            ch_communities,
            steps,
        ) = self.centrality_hiding.hide_target_node_from_community()
        return "Centrality", ch_communities, steps

    def run_greedy(self) -> Tuple[str, cdlib.NodeClustering, int]:
        """
        Evaluate the Roam Hiding algorithm on the Node Hiding task

        Returns
        -------
        Tuple[str, cdlib.NodeClustering, int]:
            Algorithm name, Set of new communities, steps
        """
        (
            gh_graph,
            gh_communities,
            steps,
        ) = self.greedy_hiding.hide_target_node_from_community()
        return "Greedy", gh_communities, steps

    ############################################################################
    #                               UTILS                                      #
    ############################################################################
    def get_nmi(
        self,
        old_communities: cdlib.NodeClustering,
        new_communities: cdlib.NodeClustering,
    ) -> float:
        """
        Compute the Normalized Mutual Information between the old and the new
        community structure

        Parameters
        ----------
        old_communities : cdlib.NodeClustering
            Community structure before deception
        new_communities : cdlib.NodeClustering
            Community structure after deception

        Returns
        -------
        float
            Normalized Mutual Information between the old and the new community
        """
        if new_communities is None:
            # The agent did not perform any rewiring, i.e. are the same communities
            return 1
        return old_communities.normalized_mutual_information(new_communities).score

    def get_new_community(self, new_community_structure: List[List[int]]) -> List[int]:
        """
        Search the community target in the new community structure after
        deception. As new community target after the action, we consider the
        community that contains the target node, if this community satisfies
        the deception constraint, the episode is finished, otherwise not.

        Parameters
        ----------
        node_target : int
            Target node to be hidden from the community
        new_community_structure : List[List[int]]
            New community structure after deception

        Returns
        -------
        List[int]
            New community target after deception
        """
        if new_community_structure is None:
            # The agent did not perform any rewiring, i.e. are the same communities
            return self.community_target
        for community in new_community_structure.communities:
            if self.node_target in community:
                return community
        raise ValueError("Community not found")

    def check_goal(self, new_community: int) -> int:
        """
        Check if the goal of hiding the target node was achieved

        Parameters
        ----------
        new_community : int
            New community of the target node

        Returns
        -------
        int
            1 if the goal was achieved, 0 otherwise
        """
        if len(new_community) == 1:
            return 1
        # Copy the communities to avoid modifying the original ones
        new_community_copy = new_community.copy()
        new_community_copy.remove(self.node_target)
        old_community_copy = self.community_target.copy()
        old_community_copy.remove(self.node_target)
        # Compute the similarity between the new and the old community
        similarity = self.agent.env.community_similarity(
            new_community_copy, old_community_copy
        )
        del new_community_copy, old_community_copy
        if similarity <= self.tau:
            return 1
        return 0

    ############################################################################
    #                               LOG                                        #
    ############################################################################
    def set_log_dict(self) -> None:
        self.log_dict = dict()

        for alg in self.evaluation_algs:
            self.log_dict[alg] = {
                "goal": [],
                "nmi": [],
                "time": [],
                "steps": [],
                "target_node": [],
                "community_len": [],
            }

        # Add environment parameters to the log dictionaryù
        self.log_dict["env"] = dict()
        self.log_dict["env"]["dataset"] = self.env_name
        self.log_dict["env"]["detection_alg"] = self.detection_alg
        self.log_dict["env"]["beta"] = self.beta
        self.log_dict["env"]["tau"] = self.tau
        self.log_dict["env"]["edge_budget"] = self.edge_budget
        self.log_dict["env"]["max_steps"] = self.max_steps

        # Add Agent Hyperparameters to the log dictionary
        # TEST Centrality
        # self.log_dict["Agent"]["lr"] = self.lr
        # self.log_dict["Agent"]["gamma"] = self.gamma
        # self.log_dict["Agent"]["lambda_metric"] = self.lambda_metric
        # self.log_dict["Agent"]["alpha_metric"] = self.alpha_metric
        # self.log_dict["Agent"]["epsilon_prob"] = self.epsilon_prob

    def save_metrics(
        self, alg: str, goal: int, nmi: float, time: float, steps: int
    ) -> dict:
        """Save the metrics of the algorithm in the log dictionary"""
        self.log_dict[alg]["goal"].append(goal)
        self.log_dict[alg]["nmi"].append(nmi)
        self.log_dict[alg]["time"].append(time)
        self.log_dict[alg]["steps"].append(steps)
        self.log_dict[alg]["target_node"].append(self.node_target)
        self.log_dict[alg]["community_len"].append(len(self.community_target))
